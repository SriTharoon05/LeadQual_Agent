"""Bounded contact checks; availability never claims guaranteed delivery."""
import json
import logging
import smtplib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from app.config import settings
from app.validators.contact_policy import CONTACT_POLICY_VERSION, dns_source, email_syntax, evaluate_contact, present

logger = logging.getLogger(__name__)
try:
    import dns.resolver
    _DNS_OK = True
except ImportError:
    _DNS_OK = False
try:
    import phonenumbers
    _PHONE_OK = True
except ImportError:
    _PHONE_OK = False


@dataclass
class ContactVerifyResult:
    policy_version: str = CONTACT_POLICY_VERSION
    checked_at: str = ""
    methods: list = field(default_factory=list)
    email_valid: bool = False
    email_deliverable: bool | None = None
    email_delivery_status: str = "unknown"
    email_notes: str = ""
    phone_valid: bool = False
    phone_e164: str = ""
    phone_carrier: str = ""
    phone_country: str = ""
    employment_confirmed: bool = False
    employment_status: str = "unclear"
    contact_quality_score: float = 0.0
    contact_available: bool = False
    raw_output: str = ""

    def to_dict(self):
        return {key: value for key, value in asdict(self).items() if key != "raw_output"}


def _email(email, checked_at):
    valid, domain = email_syntax(email)
    result = {"kind": "email", "value": email, "status": "unverified" if valid else "invalid",
              "delivery_status": "unknown", "checked_at": checked_at, "sources": [], "observations": [],
              "explanation": "Valid syntax; mailbox delivery has not been established." if valid else domain}
    if not valid or not _DNS_OK:
        return result
    resolver = dns.resolver.Resolver()
    resolver.timeout = resolver.lifetime = min(10.0, max(1.0, float(getattr(settings, "DNS_TIMEOUT_S", 5))))
    mx_host = None
    try:
        records = resolver.resolve(domain, "MX")
        exchanges = sorted((int(record.preference), str(record.exchange)) for record in records)
        result["observations"].append({"type": "MX", "domain": domain, "records": [f"{rank} {host}" for rank, host in exchanges]})
        result["sources"] = [dns_source(domain, "MX")]
        if len(exchanges) == 1 and exchanges[0][1] == ".":
            result.update(status="invalid", explanation="The domain advertises Null MX: it declares it does not accept email (RFC 7505).")
            return result
        hosts = [host.rstrip(".") for _, host in exchanges if host != "."]
        if hosts:
            mx_host = hosts[0]
            result.update(status="available", explanation="Syntax and domain mail routing are valid. The mailbox and inbox delivery are unverified.")
    except dns.resolver.NXDOMAIN:
        result.update(status="invalid", explanation="DNS returned NXDOMAIN: this domain did not exist at the recorded check time.")
        result["observations"].append({"type": "MX", "domain": domain, "response": "NXDOMAIN"})
        result["sources"] = [dns_source(domain, "MX")]
        return result
    except dns.resolver.NoAnswer:
        # RFC 5321 permits implicit MX via A/AAAA. No MX alone is not invalid.
        result["observations"].append({"type": "MX", "domain": domain, "response": "NoAnswer"})
        absent = 0
        for record_type in ("A", "AAAA"):
            try:
                answers = resolver.resolve(domain, record_type)
                result["observations"].append({"type": record_type, "domain": domain, "records": [str(answer) for answer in answers]})
                result["sources"].append(dns_source(domain, record_type))
                result.update(status="available", explanation="An implicit SMTP route exists via A/AAAA (RFC 5321). Mailbox delivery is unverified.")
                mx_host = domain
                break
            except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
                absent += 1
                result["observations"].append({"type": record_type, "domain": domain, "response": "No address record"})
                result["sources"].append(dns_source(domain, record_type))
            except Exception as exc:
                result["observations"].append({"type": record_type, "domain": domain, "response": type(exc).__name__})
        if not mx_host:
            if absent == 2:
                result.update(status="invalid", explanation="No MX or fallback A/AAAA mail route existed at this check. This is domain routing evidence, not a mailbox delivery test.")
            else:
                result["explanation"] = "Mail routing checks were inconclusive. Delivery is unknown; this is not a confirmed mailbox rejection."
    except Exception as exc:
        result["observations"].append({"type": "MX", "domain": domain, "response": type(exc).__name__})
        result["explanation"] = "DNS verification was inconclusive. Syntax is valid; delivery is unknown."
    if mx_host and bool(getattr(settings, "ENABLE_SMTP_CHECK", False)):
        try:
            timeout = min(10.0, max(1.0, float(getattr(settings, "SMTP_TIMEOUT_S", 5))))
            with smtplib.SMTP(mx_host, 25, timeout=timeout) as smtp:
                smtp.ehlo_or_helo_if_needed()
                sender_code, _ = smtp.mail("verify@ailead-verify.local")
                if sender_code == 250:
                    code, message = smtp.rcpt(email)
                    result["observations"].append({"type": "SMTP_RCPT", "server": mx_host, "code": code,
                                                   "response": message.decode("utf-8", "replace")[:300] if isinstance(message, bytes) else str(message)[:300]})
                    if code in (250, 251):
                        result["delivery_status"] = "server_accepted"
                        result["explanation"] += " The server accepted RCPT at this check; inbox delivery is still not guaranteed."
                    else:
                        result["explanation"] += " The server did not accept this probe. Sender policy, anti-abuse, temporary or recipient issues are possible; mailbox delivery remains unknown."
        except Exception as exc:
            result["observations"].append({"type": "SMTP_RCPT", "response": type(exc).__name__})
            result["explanation"] += " SMTP was inconclusive; this is not proof of an undeliverable mailbox."
    return result


def _region(country):
    if country.lower() == "uk":
        return "GB"
    if len(country) == 2:
        return country.upper() if not _PHONE_OK or country.upper() in phonenumbers.SUPPORTED_REGIONS else None
    return {"united states": "US", "united states of america": "US", "usa": "US", "united kingdom": "GB",
            "uk": "GB", "canada": "CA", "india": "IN", "australia": "AU", "germany": "DE", "france": "FR",
            "singapore": "SG", "netherlands": "NL", "uae": "AE", "united arab emirates": "AE"}.get(country.lower())


def _phone(value, region, checked_at):
    result = {"kind": "phone", "value": value, "status": "unknown", "delivery_status": "unknown",
              "checked_at": checked_at, "sources": [], "observations": [],
              "explanation": "Phone format check unavailable. No call was placed."}
    if not _PHONE_OK:
        return result
    try:
        number = phonenumbers.parse(value, region)
        valid = phonenumbers.is_valid_number(number)
        possible = phonenumbers.is_possible_number(number)
        e164 = phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)
        result["observations"] = [{"type": "phone_numbering_plan", "e164": e164, "format_valid": valid, "possible": possible}]
        result.update(status="available" if valid else "unknown" if possible else "invalid",
                      explanation="Number fits the numbering plan. Ownership and call reachability are unverified; no call was placed." if valid else
                      "Number format is possible but unconfirmed. Ownership and call reachability are unknown." if possible else
                      "Number does not fit the numbering plan. This is a format finding, not a failed call.")
    except phonenumbers.NumberParseException:
        result.update(status="unknown" if not region and not value.startswith("+") else "invalid",
                      explanation="Number cannot be parsed with the available country information. Call reachability is unknown.")
    return result


def verify_contact(lead_data, web_result=None, icp_prompt=""):
    data = lead_data if isinstance(lead_data, dict) else {}
    result = ContactVerifyResult(checked_at=datetime.now(timezone.utc).isoformat())
    email = present(data.get("Email Address"))
    if email:
        try:
            result.methods.append(_email(email, result.checked_at))
        except Exception:
            valid, _ = email_syntax(email)
            result.methods.append({"kind": "email", "value": email, "status": "unverified" if valid else "invalid",
                                   "delivery_status": "unknown", "checked_at": result.checked_at, "sources": [], "observations": [],
                                   "explanation": "Verification could not finish; inbox delivery is unknown."})
    country = present(data.get("Contact Country")) or present(data.get("Company Country"))
    region = _region(country) if country else None
    # A bad direct number must not hide a good main contact number.
    for value in dict.fromkeys(present(data.get(name)) for name in ("Direct Phone Number", "Phone Number")):
        if value:
            try:
                result.methods.append(_phone(value, region, result.checked_at))
            except Exception:
                result.methods.append({"kind": "phone", "value": value, "status": "unknown", "delivery_status": "unknown",
                                       "checked_at": result.checked_at, "sources": [], "observations": [], "explanation": "Phone check was inconclusive. No call was placed."})
    emails = [method for method in result.methods if method["kind"] == "email"]
    phones = [method for method in result.methods if method["kind"] == "phone"]
    result.email_valid = any(method["status"] in {"available", "unverified"} for method in emails)
    result.phone_valid = any(method["status"] == "available" for method in phones)
    result.email_notes = emails[0]["explanation"] if emails else "No email address provided"
    result.email_delivery_status = emails[0]["delivery_status"] if emails else "unknown"
    result.employment_status = getattr(web_result, "employment_status", "unclear")
    result.employment_confirmed = result.employment_status == "current"
    availability = evaluate_contact(result.to_dict(), icp_prompt)
    result.contact_quality_score = availability["score"] / 10
    result.contact_available = availability["verdict"] == "match"
    result.raw_output = json.dumps(result.to_dict())
    return result
