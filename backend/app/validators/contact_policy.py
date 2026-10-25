"""Pure availability rules. Delivery/telephone reachability remain unproven."""
import re
from urllib.parse import urlencode

CONTACT_POLICY_VERSION = "availability_v2"
try:
    from email_validator import EmailNotValidError, validate_email
except ImportError:
    validate_email = None


def present(value):
    value = str(value or "").strip()
    return "" if value.lower() in {"", "none", "null", "n/a", "na", "unknown", "not available", "-", "--"} else value


def email_syntax(value):
    if validate_email is not None:
        try:
            checked = validate_email(value, check_deliverability=False, allow_quoted_local=True)
            return True, checked.ascii_domain or checked.domain
        except EmailNotValidError as exc:
            return False, str(exc)
    if not value or len(value) > 254 or value.count("@") != 1 or re.search(r"[\s\x00-\x1f\x7f]", value):
        return False, "Invalid or unsupported email syntax."
    local, domain = value.rsplit("@", 1)
    try:
        domain = domain.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return False, "Invalid email domain syntax."
    labels = domain.split(".")
    if (len(labels) < 2 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
            or not re.fullmatch(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+", local) or len(local) > 64
            or local.startswith(".") or local.endswith(".") or ".." in local):
        return False, "Invalid or unsupported email syntax."
    return True, domain


def contact_requirement(prompt):
    modes = set()
    for clause in re.split(r"[\n;.!?]", str(prompt or "").lower()):
        channels = {"email": r"(?:e-?mails?(?: addresses?)?|mails?(?: addresses?)?)", "phone": r"(?:phones?(?: numbers?)?|telephone|mobile|direct dial)"}
        present_channels = {kind: bool(re.search(rf"\b{pattern}\b", clause)) for kind, pattern in channels.items()}
        optional = {kind: bool(re.search(rf"\b{pattern}\b[^,;]{{0,15}}\b(?:optional|not required|not necessary|not mandatory|not needed)\b|\b(?:no need(?: of| for)?|do not require|don't require|do not need|don't need)\s+(?:(?:a|an|the|valid)\s+)*{pattern}\b|\bno\s+{pattern}\b\s+(?:is\s+)?required\b", clause)) for kind, pattern in channels.items()}
        email = present_channels["email"] and not optional["email"]
        phone = present_channels["phone"] and not optional["phone"]
        if email and phone:
            mentions = sorted((match.start(), match.end()) for pattern in channels.values() for match in re.finditer(rf"\b{pattern}\b", clause))
            between = clause[mentions[0][0]:mentions[-1][1]] if mentions else ""
            if re.search(r"\b(?:or|either)\b", between) or re.search(r"\b(?:either|at least one|any one)\b", clause):
                continue
        if re.search(r"\b(?:must|required|require|requires|mandatory|need|needs|only|with|having)\b", clause):
            if email:
                modes.add("email")
            if phone:
                modes.add("phone")
    return "both" if len(modes) == 2 else next(iter(modes), "any")


def evaluate_contact(data, prompt=""):
    data = data if isinstance(data, dict) else {}
    mode = contact_requirement(prompt)
    methods = data.get("methods") if isinstance(data.get("methods"), list) else []
    available = {method.get("kind") for method in methods if isinstance(method, dict) and isinstance(method.get("kind"), str) and method.get("kind") in {"email", "phone"}
                 and present(method.get("value")) and isinstance(method.get("status"), str) and method.get("status") in {"available", "unverified"}}
    unknown = {method.get("kind") for method in methods if isinstance(method, dict) and isinstance(method.get("kind"), str) and method.get("kind") in {"email", "phone"}
               and present(method.get("value")) and method.get("status") == "unknown"}
    needed = {"email", "phone"} if mode == "both" else {mode} if mode != "any" else set()
    strict_email = bool(re.search(r"\b(?:must|required|mandatory|require)\b[^.;\n]{0,60}\b(?:deliverable|verified|confirmed)\s+(?:(?:work|business)\s+)?e-?mails?\b", str(prompt), re.I))
    if strict_email and "email" in available:
        available.remove("email")
        unknown.add("email")
    passed = needed.issubset(available) if needed else bool(available)
    pending = not passed and (bool(needed & unknown) if needed else bool(unknown))
    requirement = {"any": "At least one available email or phone", "email": "Email explicitly required by the ICP",
                   "phone": "Phone explicitly required by the ICP", "both": "Both email and phone explicitly required by the ICP"}[mode]
    if strict_email:
        requirement += "; verified mailbox delivery explicitly required but not established by these checks"
    return {"mode": mode, "verdict": "match" if passed else "unclear" if pending else "mismatch",
            "score": 100.0 if passed else 50.0 if pending else 0.0,
            "requirement": requirement, "available_channels": sorted(available)}


def saved_contact(lead, raw):
    """Interpret legacy flags without inventing negative delivery evidence."""
    raw = raw if isinstance(raw, dict) else {}
    lead = lead if isinstance(lead, dict) else {}
    if raw.get("policy_version") == CONTACT_POLICY_VERSION and isinstance(raw.get("methods"), list):
        return raw
    methods = []
    email = present(lead.get("Email Address"))
    if email:
        valid, detail = email_syntax(email)
        negative = bool(re.search(r"\b(?:invalid email syntax|NXDOMAIN|null MX)\b", str(raw.get("email_notes") or ""), re.I))
        inconclusive = bool(re.search(r"(?:timeout|unavailable|NoNameservers|LifetimeTimeout|disabled|Syntax valid)", str(raw.get("email_notes") or ""), re.I))
        status = "invalid" if not valid or negative else "unverified" if raw.get("email_valid") is True or inconclusive else "unknown"
        methods.append({"kind": "email", "value": email, "status": status,
                        "delivery_status": "unknown", "checked_at": None, "observations": [], "sources": [],
                        "explanation": detail if not valid else "A legacy check recorded a domain failure; refresh for current DNS evidence." if negative else
                        "Valid syntax. Legacy checks do not establish mailbox delivery; a false delivery flag is not proof of rejection."})
    phones = list(filter(None, dict.fromkeys(present(lead.get(key)) for key in ("Direct Phone Number", "Phone Number"))))
    for phone in phones:
        # The previous verifier checked direct phone first, otherwise main.
        valid = bool(raw.get("phone_valid")) and phone == phones[0]
        methods.append({"kind": "phone", "value": phone, "status": "available" if valid else "unknown",
                        "delivery_status": "unknown", "checked_at": None, "observations": [], "sources": [],
                        "explanation": "Saved format check passed; call reachability was not tested." if valid else
                        "Individual format evidence was not saved; refresh to verify. No call reachability is established."})
    return {**raw, "policy_version": CONTACT_POLICY_VERSION, "methods": methods, "email_deliverable": None,
            "email_delivery_status": "unknown"}


def contact_proof(data, prompt):
    data = data if isinstance(data, dict) else {}
    check = evaluate_contact(data, prompt)
    methods = [method for method in (data.get("methods") if isinstance(data.get("methods"), list) else []) if isinstance(method, dict)]
    descriptions = [f"{str(method.get('kind') or 'Contact').title()}: {method.get('value', '')} ({method.get('status', 'unknown')})" for method in methods]
    return {"field": "contact_method", "label": "Contact availability", "value": "; ".join(descriptions) or "No email or phone available",
            "status": "unclear" if check["verdict"] == "unclear" else "confirmed", "requirement": check["requirement"],
            "verdict": check["verdict"], "required": True, "explanation": "One permitted route is enough. Syntax/DNS/numbering-plan checks do not guarantee inbox delivery, ownership, or that a call will connect.",
            "reasoning": "Available channels: " + (", ".join(check["available_channels"]) or "none established"),
            "checked_at": data.get("checked_at"), "methods": methods,
            "sources": list({source["url"]: source for method in methods
                for source in (method.get("sources") if isinstance(method.get("sources"), list) else [])
                if isinstance(source, dict) and isinstance(source.get("url"), str)}.values())}


def dns_source(domain, record_type):
    return {"url": "https://dns.google/resolve?" + urlencode({"name": domain, "type": record_type}),
            "title": f"Inspect {record_type} DNS for {domain}", "excerpt": "Live DNS; compare with the timestamped observation saved below."}
