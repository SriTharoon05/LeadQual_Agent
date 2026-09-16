"""
validators/contact_verifier.py
───────────────────────────────
Stage 3 — Contact Quality Check.  (HARDENED, production-grade edition)

Three independent, best-effort checks per lead:
  1. Email      — syntax + DNS MX lookup (+ optional SMTP RCPT handshake)
  2. Phone      — parse/validate via `phonenumbers`
  3. Employment — from Stage 2 web research result

Design principles:
  • NEVER crash the Celery task — every check is isolated + guarded.
  • NEVER hang — DNS and SMTP have hard timeouts.
  • Cloud-aware scoring — many hosts (incl. Azure) BLOCK outbound port 25, so
    the SMTP deliverability probe reliably fails there. SMTP is therefore
    OPT-IN (settings.ENABLE_SMTP_CHECK, default off), and the quality score
    ADAPTS so a lead isn't silently penalised for an impossible check.

Import:
    from app.validators.contact_verifier import verify_contact
    result = verify_contact(lead_data, web_result)
"""

import json
import logging
import re
import smtplib
import socket
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ── Optional deps guarded so an import failure can't kill the pipeline ─────────
try:
    import dns.resolver
    import dns.exception
    _DNS_OK = True
except Exception as e:  # pragma: no cover
    _DNS_OK = False
    logger.warning(f"dnspython unavailable — MX checks disabled: {e}")

try:
    import phonenumbers
    from phonenumbers import carrier, geocoder, PhoneNumberFormat
    _PHONE_OK = True
except Exception as e:  # pragma: no cover
    _PHONE_OK = False
    logger.warning(f"phonenumbers unavailable — phone checks disabled: {e}")

# ── Config (with safe fallbacks if settings lacks the keys) ───────────────────
try:
    from app.config import settings
    _ENABLE_SMTP  = bool(getattr(settings, "ENABLE_SMTP_CHECK", False))
    _SMTP_TIMEOUT = float(getattr(settings, "SMTP_TIMEOUT_S", 5))
    _DNS_TIMEOUT  = float(getattr(settings, "DNS_TIMEOUT_S", 5))
except Exception:
    _ENABLE_SMTP, _SMTP_TIMEOUT, _DNS_TIMEOUT = False, 5.0, 5.0

_SMTP_FROM_ADDR = "verify@ailead-verify.local"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ── Safe helpers ──────────────────────────────────────────────────────────────
def _s(value, default: str = "") -> str:
    """None-safe stripped string."""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    try:
        return str(value).strip()
    except Exception:
        return default


# ── Result dataclass ──────────────────────────────────────────────────────────
@dataclass
class ContactVerifyResult:
    email_valid: bool = False
    email_deliverable: bool = False
    email_notes: str = ""
    phone_valid: bool = False
    phone_e164: str = ""
    phone_carrier: str = ""
    phone_country: str = ""
    employment_confirmed: bool = False
    employment_status: str = "unclear"
    contact_quality_score: float = 0.0
    raw_output: str = ""

    def to_dict(self) -> dict:
        return {
            "email_valid":           self.email_valid,
            "email_deliverable":     self.email_deliverable,
            "email_notes":           self.email_notes,
            "phone_valid":           self.phone_valid,
            "phone_e164":            self.phone_e164,
            "phone_carrier":         self.phone_carrier,
            "phone_country":         self.phone_country,
            "employment_confirmed":  self.employment_confirmed,
            "employment_status": self.employment_status,
            "contact_quality_score": self.contact_quality_score,
        }


# ── Public API ────────────────────────────────────────────────────────────────
def verify_contact(lead_data: dict, web_result=None) -> ContactVerifyResult:
    """
    Run Stage 3 contact verification. Never raises — each check is isolated.
    """
    result = ContactVerifyResult()

    if not isinstance(lead_data, dict):
        logger.warning("verify_contact: lead_data is not a dict — skipping")
        result.email_notes = "Invalid lead data"
        result.raw_output = json.dumps(result.to_dict())
        return result

    # ── 1. Email ──────────────────────────────────────────────────────────────
    email = _s(lead_data.get("Email Address"))
    if email:
        try:
            result.email_valid, result.email_deliverable, result.email_notes = _check_email(email)
        except Exception as e:
            logger.warning(f"email check failed for '{email}': {e}")
            result.email_notes = f"Email check error: {e}"
    else:
        result.email_notes = "No email address provided"

    # ── 2. Phone ──────────────────────────────────────────────────────────────
    phone_raw = _s(lead_data.get("Direct Phone Number")) or _s(lead_data.get("Phone Number"))
    country_hint = _s(lead_data.get("Contact Country")) or "US"
    if phone_raw:
        try:
            (result.phone_valid, result.phone_e164,
             result.phone_carrier, result.phone_country) = _check_phone(phone_raw, country_hint)
        except Exception as e:
            logger.warning(f"phone check failed for '{phone_raw}': {e}")

    # ── 3. Employment cross-check ─────────────────────────────────────────────
    if web_result is not None:
        try:
            result.employment_status = getattr(web_result, "employment_status", "unclear")
            result.employment_confirmed = result.employment_status == "current"
        except Exception:
            result.employment_confirmed = False

    # ── 4. Composite quality score (adaptive) ─────────────────────────────────
    result.contact_quality_score = _score(result, email_present=bool(email))

    try:
        result.raw_output = json.dumps(result.to_dict(), default=str)
    except Exception:
        result.raw_output = "{}"

    logger.debug(
        f"Contact verify [{_s(lead_data.get('Contact Name')) or '?'}] "
        f"email_valid={result.email_valid} deliverable={result.email_deliverable} "
        f"phone={result.phone_valid} employment={result.employment_confirmed} "
        f"quality={result.contact_quality_score}"
    )
    return result


# ── Email check ───────────────────────────────────────────────────────────────
def _check_email(email: str) -> tuple[bool, bool, str]:
    """
    Returns (email_valid, email_deliverable, notes).
      email_valid       = syntactically valid AND domain has an MX record.
      email_deliverable = SMTP RCPT accepted (ONLY attempted if enabled).
    """
    if not _EMAIL_RE.match(email):
        return False, False, "Invalid email syntax"

    domain = email.split("@", 1)[1]

    if not _DNS_OK:
        # Can't verify MX → treat as syntactically-valid only (don't over-claim).
        return True, False, "Syntax valid (MX check unavailable)"

    # DNS MX lookup with a hard timeout so it can never hang the task.
    try:
        resolver = dns.resolver.Resolver()
        resolver.timeout = _DNS_TIMEOUT
        resolver.lifetime = _DNS_TIMEOUT
        mx_records = resolver.resolve(domain, "MX")
        mx_host = str(sorted(mx_records, key=lambda r: r.preference)[0].exchange).rstrip(".")
    except Exception as e:
        return False, False, f"No MX record for '{domain}': {type(e).__name__}"

    # SMTP deliverability — opt-in only (port 25 is blocked on many cloud hosts).
    if not _ENABLE_SMTP:
        return True, False, f"MX: {mx_host} (SMTP probe disabled)"

    deliverable = _smtp_check(email, mx_host)
    notes = f"MX: {mx_host}" + ("" if deliverable else " | SMTP: rejected/timeout/blocked")
    return True, deliverable, notes


def _smtp_check(email: str, mx_host: str) -> bool:
    """RCPT TO probe (no mail sent). Never raises — any error → False."""
    try:
        with smtplib.SMTP(mx_host, 25, timeout=_SMTP_TIMEOUT) as smtp:
            smtp.ehlo_or_helo_if_needed()
            smtp.mail(_SMTP_FROM_ADDR)
            code, _ = smtp.rcpt(email)
            return code in (250, 251)
    except (smtplib.SMTPException, socket.timeout, OSError):
        return False


# ── Phone check ───────────────────────────────────────────────────────────────
def _check_phone(raw: str, country_hint: str = "US") -> tuple[bool, str, str, str]:
    """Returns (phone_valid, e164, carrier_name, country_name)."""
    if not _PHONE_OK:
        return False, "", "", ""

    region = _country_to_iso(country_hint)
    try:
        parsed = phonenumbers.parse(raw, region)
    except Exception:
        # Retry once with no region in case the number is already E.164 (+…).
        try:
            parsed = phonenumbers.parse(raw, None)
        except Exception:
            return False, "", "", ""

    try:
        if not phonenumbers.is_valid_number(parsed):
            return False, "", "", ""
        e164  = phonenumbers.format_number(parsed, PhoneNumberFormat.E164)
        cname = carrier.name_for_number(parsed, "en") or ""
        cty   = geocoder.country_name_for_number(parsed, "en") or ""
        return True, e164, cname, cty
    except Exception:
        return False, "", "", ""


def _country_to_iso(country: str) -> str:
    _MAP = {
        "UNITED STATES": "US", "UNITED STATES OF AMERICA": "US", "USA": "US", "US": "US",
        "UNITED KINGDOM": "GB", "UK": "GB", "GB": "GB", "ENGLAND": "GB",
        "CANADA": "CA", "CA": "CA",
        "AUSTRALIA": "AU", "AU": "AU",
        "INDIA": "IN", "IN": "IN",
        "GERMANY": "DE", "DE": "DE",
        "FRANCE": "FR", "FR": "FR",
        "NETHERLANDS": "NL", "NL": "NL",
        "SINGAPORE": "SG", "SG": "SG",
        "UAE": "AE", "UNITED ARAB EMIRATES": "AE", "AE": "AE",
    }
    return _MAP.get(_s(country).upper(), "US")


# ── Quality scorer (adaptive to whether SMTP is available) ────────────────────
def _score(r: ContactVerifyResult, email_present: bool) -> float:
    """
    Compute a 0–10 contact-quality score.

    Base weights: email_valid 2 · deliverable 3 · phone 2 · employment 3.

    ADAPTIVE: when SMTP is disabled (deliverability can't be measured), the
    3 "deliverable" points are redistributed so a lead with a valid MX email
    isn't silently capped 3 points lower for a check we never ran. Without this
    fix, EVERY lead on Azure loses 3 points → skews all downstream scoring.
    """
    score = 0.0
    if r.phone_valid:
        score += 2.0
    if r.employment_confirmed:
        score += 3.0

    smtp_measured = _ENABLE_SMTP and _DNS_OK
    if smtp_measured:
        if r.email_valid:
            score += 2.0
        if r.email_deliverable:
            score += 3.0
    else:
        # Deliverability not measurable → weight a valid MX email as worth the
        # full email allotment (2 + 3 = 5) so scoring stays balanced.
        if r.email_valid:
            score += 5.0

    return round(min(score, 10.0), 1)
