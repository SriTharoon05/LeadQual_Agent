"""Resolve names against the whole job, without silently correcting a typo."""

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from collections import OrderedDict
import re
from threading import RLock
from time import monotonic
import unicodedata

from app.models import Job, Lead


def normalized(value: str) -> str:
    text = unicodedata.normalize("NFKC", value if isinstance(value, str) else "").casefold()
    return " ".join(re.findall(r"[^\W_]+", text, flags=re.UNICODE))


_GENERIC_NAMES = {
    "a", "an", "all", "and", "in", "it", "is", "us", "the", "to", "yes",
    "no", "high", "medium", "low", "data", "lead", "leads", "company",
    "companies", "contact", "contacts", "email", "phone", "notes", "total",
    "software", "services", "technology", "sales", "marketing", "finance",
}
_ENTITY_STOP = {
    "email", "emails", "phone", "phones", "score", "scores", "priority",
    "notes", "note", "revenue", "location", "industry", "employees", "size",
    "all", "each", "every", "this", "that", "these", "those", "them", "their",
    "him", "her", "it", "me", "us", "high priority", "low priority",
    "medium priority", "next", "more", "the company", "the person",
    "that company", "this company", "his company", "her company", "their company",
    "that person", "this person", "his contact", "her contact", "their contact",
    "companies", "contacts", "leads", "records", "rows", "the leads",
}


@dataclass
class Registry:
    companies: dict[str, list[str]] = field(default_factory=dict)
    people: dict[str, list[dict]] = field(default_factory=dict)
    by_id: dict[str, dict] = field(default_factory=dict)
    candidate_buckets: dict[tuple[str, str], set[str]] = field(default_factory=dict)


_registries = OrderedDict()
_registry_lock = RLock()
_REGISTRY_TTL = 15.0
_REGISTRY_RECORD_BUDGET = 100_000


def load_registry(db, job_id: str) -> Registry:
    """Read current names from every lead, including leads on other table pages.

    Failures deliberately propagate to the API; a DB outage is not an empty job.
    Only identifiers and names are loaded, never every lead's research payload.
    """
    key = str(job_id).lower()
    job = db.get(Job, job_id)
    cacheable = bool(job and job.status == "completed")
    if cacheable:
        with _registry_lock:
            cached = _registries.get(key)
            if cached and monotonic() - cached[0] < _REGISTRY_TTL:
                _registries.move_to_end(key)
                return cached[1]
            _registries.pop(key, None)
    registry = Registry()
    rows = db.query(Lead.id, Lead.company_name, Lead.contact_name, Lead.row_index).filter(
        Lead.job_id == job_id).all()
    for lead_id, company, person, row_index in rows:
        record = {"id": str(lead_id).lower(), "company": company or "", "person": person or "",
                  "row_number": row_index + 1 if row_index is not None else None}
        registry.by_id[str(lead_id).lower()] = record
        company_key = normalized(company)
        if company_key:
            names = registry.companies.setdefault(company_key, [])
            if company not in names:
                names.append(company)
        person_key = normalized(person)
        if person_key:
            registry.people.setdefault(person_key, []).append(record)
    for kind, names in (("company", registry.companies), ("person", registry.people)):
        for name in names:
            registry.candidate_buckets.setdefault((kind, "token:" + name.split()[0]), set()).add(name)
            for token in name.split():
                if len(token) >= 2:
                    registry.candidate_buckets.setdefault((kind, "first:" + token[0]), set()).add(name)
                    registry.candidate_buckets.setdefault((kind, "second:" + token[1]), set()).add(name)
    if cacheable and len(registry.by_id) <= _REGISTRY_RECORD_BUDGET:
        with _registry_lock:
            _registries[key] = (monotonic(), registry)
            _registries.move_to_end(key)
            while len(_registries) > 8 or sum(len(item[1].by_id) for item in _registries.values()) > _REGISTRY_RECORD_BUDGET:
                _registries.popitem(last=False)
    return registry


def _present(key: str, message_key: str) -> bool:
    return bool(key and f" {key} " in f" {message_key} ")


def exact_mentions(registry: Registry, message: str) -> list[dict]:
    """Full name matches only; unrelated question words never become a person."""
    message_key = normalized(message)
    explicit_company = bool(re.search(r"\b(?:company|organization|organisation)\b", message, re.I))
    mentions = []
    for kind, names in (("company", registry.companies), ("person", registry.people)):
        candidates = set()
        for token in message_key.split():
            candidates.update(registry.candidate_buckets.get((kind, "token:" + token), ()))
        matches = [key for key in candidates if _present(key, message_key)
                   and (key not in _GENERIC_NAMES or (kind == "company" and explicit_company))]
        # "Acme" inside "Acme Holdings" should not create two different scopes.
        matches.sort(key=len, reverse=True)
        accepted = []
        for key in matches:
            if any(_present(key, longer) for longer in accepted):
                continue
            accepted.append(key)
            canonical = names[key][0] if kind == "company" else names[key][0]["person"]
            mentions.append({"kind": kind, "value": canonical})
    result, seen = [], set()
    for mention in mentions:
        key = normalized(mention["value"])
        if key in seen:
            continue
        seen.add(key)
        if key in registry.companies and key in registry.people:
            company_cue = re.search(rf"\b(?:company|organization|organisation)\s+(?:(?:named|called)\s+)?{re.escape(key)}(?!\w)", message_key)
            person_cue = re.search(rf"\b(?:person|contact)\s+(?:(?:named|called)\s+)?{re.escape(key)}(?!\w)", message_key)
            kind = "company" if company_cue and not person_cue else "person" if person_cue and not company_cue else "unknown"
            result.append({"kind": kind, "value": mention["value"]})
        else:
            result.append(mention)
    return result


def heuristic_mentions(message: str) -> list[dict]:
    """Extract explicit name-shaped phrases, never arbitrary leftover words."""
    mentions = []
    patterns = [
        ("company", r"\b(?:company|organization|organisation)\s+(?:named\s+|called\s+)?[\"']?(.+?)(?:[\"']|[?!;]|$)"),
        ("person", r"\b(?:person|contact)\s+(?:named\s+|called\s+)[\"']?(.+?)(?:[\"']|[?!;]|$)"),
        ("unknown", r"\b(?:about|for|from|at)\s+[\"']?(.+?)(?:[\"']|[?!;]|$)"),
    ]
    for kind, pattern in patterns:
        for match in re.finditer(pattern, message, re.I):
            candidate = match.group(1).strip(" .,:\"'")
            # "at Acme and their email" -> Acme; preserve "Johnson and Johnson".
            candidate = re.split(
                r"\s+(?:with|whose|who|where|which|and\s+(?:their|his|her|show|give))\b",
                candidate, maxsplit=1, flags=re.I)[0].strip()
            candidate = re.sub(r"^(?:the\s+)?(?:company|person)\s+", "", candidate, flags=re.I)
            key = normalized(candidate)
            if not key or key in _ENTITY_STOP or len(key.split()) > 8 or key.startswith("row "):
                continue
            # 'company size of that' is a field/follow-up, not a company name.
            if re.match(r"^(?:size|location|revenue|industry|website|description|email|phone|name|details)\b", key):
                continue
            if re.fullmatch(r"(?:(?:of|the|that|this|their|his|her|its|same|company|person|contact)\s*)+", key):
                continue
            # Filter clauses are not entity names, e.g. "for high priority leads".
            if re.search(r"\b(?:priority|score|reviewed|unreviewed|records|rows|leads|contacts)\b", key):
                continue
            mentions.append({"kind": kind, "value": candidate[:255],
                             "cue": match.group(0).split()[0].lower()})
    return mentions


def _company_choice(registry: Registry, key: str) -> dict:
    value = registry.companies[key][0]
    return {"kind": "company", "value": value, "label": value}


def _person_choices(records: list[dict]) -> list[dict]:
    choices = []
    for record in records[:8]:
        suffix = f" — {record['company']}" if record["company"] else ""
        if record["row_number"] is not None:
            suffix += f" (row {record['row_number']})"
        choices.append({"kind": "person", "value": record["id"],
                        "label": f"{record['person']}{suffix}"})
    return choices


def _nearby(registry: Registry, key: str, kind: str) -> list[dict]:
    """Offer partial/typo candidates, but leave selection to the user."""
    kinds = [kind] if kind in {"company", "person"} else ["company", "person"]
    scored = []
    for entity_kind in kinds:
        names = registry.companies if entity_kind == "company" else registry.people
        candidates = set()
        for token in key.split():
            if len(token) >= 2:
                candidates.update(registry.candidate_buckets.get((entity_kind, "first:" + token[0]), ()))
                candidates.update(registry.candidate_buckets.get((entity_kind, "second:" + token[1]), ()))
        # Small registries and unusual Unicode/short names retain full recall.
        pool = candidates if candidates and len(names) > 1000 else names
        threshold = 0.65 if len(key) >= 5 else 0.78

        def score_names(values):
            best_here = 0.0
            for name in values:
                if name in _GENERIC_NAMES:
                    continue
                # Cheap length/character bounds avoid edit matching every long
                # company name, while genuine short prefixes retain recall.
                partial = _present(key, name) and len(key) >= 3
                if not partial and 2 * min(len(key), len(name)) / max(1, len(key) + len(name)) < threshold:
                    continue
                matcher = SequenceMatcher(None, key, name)
                if not partial and matcher.quick_ratio() < threshold:
                    continue
                ratio = matcher.ratio()
                if ratio >= threshold or partial:
                    score = max(ratio, 0.86 if partial else 0.0)
                    scored.append((score, entity_kind, name))
                    best_here = max(best_here, score)
            return best_here

        best_here = score_names(pool)
        if pool is candidates and best_here < 0.9:
            # A typo in the first two letters can miss both buckets. Broaden the
            # suggestion search; never quietly choose a weak bucket candidate.
            score_names(name for name in names if name not in candidates)
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    if not scored:
        return []
    best = scored[0][0]
    choices = []
    for score, entity_kind, name in scored[:8]:
        if score < best - 0.18:
            continue
        choices.extend([_company_choice(registry, name)] if entity_kind == "company"
                       else _person_choices(registry.people[name]))
    unique = {(choice["kind"], choice["value"]): choice for choice in choices}
    return list(unique.values())[:8]


def resolve(registry: Registry, mentions: list[dict], selection: dict | None = None) -> dict:
    """Return exact company names/lead IDs or a user-visible clarification."""
    companies, person_ids = [], []
    if len({normalized(item.get("value", "")) for item in mentions}) > 12:
        return {"clarification": "Please compare no more than 12 named companies or contacts at once, or use a job-wide filter/count.", "choices": []}
    confirmed_key = None
    unresolved_keys = {normalized(item.get("value", "")) for item in mentions
                       if normalized(item.get("value", ""))
                       and normalized(item.get("value", "")) not in registry.companies
                       and normalized(item.get("value", "")) not in registry.people
                       and normalized(item.get("value", "")) not in _ENTITY_STOP}
    if selection and len(unresolved_keys) > 1:
        return {"clarification": "There is more than one unresolved name in this question. Please ask about one company or contact at a time so I can verify each match.",
                "choices": []}
    if selection:
        value = selection.get("value")
        if selection.get("kind") == "company":
            confirmed_key = normalized(value)
            if confirmed_key not in registry.companies:
                return {"clarification": "That company is no longer available. Please choose a current record.", "choices": []}
            companies.extend(registry.companies[confirmed_key])
        elif selection.get("kind") == "person":
            person = registry.by_id.get(str(value))
            if not person:
                return {"clarification": "That contact is no longer available. Please choose a current record.", "choices": []}
            person_ids.append(person["id"])
            confirmed_key = normalized(person["person"])

    exact_company_keys = {normalized(item.get("value", "")) for item in mentions
                          if normalized(item.get("value", "")) in registry.companies}
    exact_person_keys = {normalized(item.get("value", "")) for item in mentions
                         if normalized(item.get("value", "")) in registry.people}
    ambiguous_people = [key for key in exact_person_keys if len([
        record for record in registry.people[key]
        if not exact_company_keys or normalized(record["company"]) in exact_company_keys]) > 1]
    if len(ambiguous_people) > 1:
        return {"clarification": "More than one contact name in this comparison matches multiple records. Please ask about one contact at a time, including the company or original row number.", "choices": []}
    for mention in mentions[:12]:
        key = normalized(mention.get("value", ""))
        kind = mention.get("kind", "unknown")
        if not key or key in _ENTITY_STOP:
            continue
        if selection and (key == confirmed_key or not (key in registry.companies or key in registry.people)):
            # A validated choice replaces the original misspelling; other exact
            # names in a comparison remain independently scoped.
            continue
        company_matches = registry.companies.get(key) if kind != "person" else None
        person_matches = registry.people.get(key) if kind != "company" else None
        if not company_matches and kind != "person":
            # Punctuation variants such as RCM&D/RCMD are the same literal
            # spelling. Only a unique compact spelling is accepted as exact.
            compact = key.replace(" ", "")
            aliases = [value for name, value in registry.companies.items() if name.replace(" ", "") == compact]
            if len(aliases) == 1:
                company_matches = aliases[0]
        if company_matches and person_matches and kind == "unknown":
            return {"clarification": f"“{mention['value']}” matches a company and a contact. Which did you mean?",
                    "choices": [_company_choice(registry, key), *_person_choices(person_matches)][:8]}
        if company_matches:
            companies.extend(company_matches)
            continue
        if person_matches:
            narrowed = [record for record in person_matches
                        if not exact_company_keys or normalized(record["company"]) in exact_company_keys]
            if len(narrowed) > 1:
                return {"clarification": f"More than one record is named {mention['value']}. Which contact did you mean?",
                        "choices": _person_choices(narrowed)}
            person_ids.extend(record["id"] for record in narrowed)
            continue
        # A phrase containing an already resolved full name is just syntax.
        if any(_present(other, key) for other in exact_company_keys | exact_person_keys):
            continue
        choices = _nearby(registry, key, kind)
        if choices:
            return {"clarification": f"I couldn't find an exact match for “{mention['value']}”. Did you mean one of these?",
                    "choices": choices}
        return {"clarification": f"I couldn't find “{mention['value']}” in this job. Please provide the company, contact name, email, or row number.",
                "choices": []}
    return {"companies": list(dict.fromkeys(companies)),
            "person_ids": list(dict.fromkeys(person_ids))}
