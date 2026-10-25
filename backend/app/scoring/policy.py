"""One deterministic qualification policy for processing, refresh and backfill."""
import re

from app.validators.contact_policy import evaluate_contact
from app.validators.evidence import FIELDS, normalize_growth, safe_url

SCORING_VERSION = "icp_contact_growth_v2"
WEIGHTS = {"icp": 0.60, "contact": 0.25, "growth": 0.15}


def priority_for(score):
    return "High" if score >= 80 else "Medium" if score >= 50 else "Low"


def validated_checks(raw, evidence, prompt="", contact=None):
    """Discard arbitrary model disqualifiers; keep the displayed comparisons."""
    raw = raw if isinstance(raw, dict) else {}
    evidence = evidence if isinstance(evidence, dict) else {}
    allowed = {source.get("url", "").rstrip("/") for fact in evidence.values() if isinstance(fact, dict)
               for source in (fact.get("sources") if isinstance(fact.get("sources"), list) else [])
               if isinstance(source, dict) and safe_url(source.get("url"))}
    result = {}
    for key in FIELDS:
        item = raw.get(key) if isinstance(raw.get(key), dict) else {}
        requirement = str(item.get("requirement") or "Not specified")[:1000]
        verdict = item.get("verdict", "unclear")
        if not isinstance(verdict, str) or verdict not in {"match", "mismatch", "unclear", "not_specified"}:
            verdict = "unclear"
        optional = bool(re.match(r"\s*(?:prefer|prioriti[sz]e|ideally|optional|nice to have)\b", requirement, re.I))
        required = verdict != "not_specified" and not optional
        fact = evidence.get(key) if isinstance(evidence.get(key), dict) else {}
        sources = fact.get("sources") if isinstance(fact.get("sources"), list) else []
        proven = fact.get("status") == "confirmed" and any(isinstance(source, dict) and safe_url(source.get("url")) and source.get("excerpt") for source in sources)
        if key == "employment":
            required, requirement = True, "Currently employed at the uploaded company"
            verdict = {"current": "match", "former": "mismatch"}.get(fact.get("value"), "unclear") if proven else "unclear"
        elif verdict != "not_specified" and not proven:
            verdict = "unclear"
        reasoning = str(item.get("reasoning") or "")[:1500]
        if verdict == "unclear":
            reasoning = "The saved evidence does not establish whether this requirement is met."
        result[key] = {"requirement": requirement, "verdict": verdict, "required": required, "reasoning": reasoning}
    # Optional extra mandatory criteria must carry an actual ICP quote and proof.
    compact_prompt = " ".join(str(prompt).split()).casefold()
    for key, item in raw.items():
        if not key.startswith("additional_") or not isinstance(item, dict):
            continue
        quote = " ".join(str(item.get("requirement_quote") or "").split())
        if not quote or quote.casefold() not in compact_prompt:
            continue
        source_items = item.get("sources") if isinstance(item.get("sources"), list) else []
        sources = [{"url": safe_url(source.get("url")), "title": str(source.get("title") or "Source")[:160], "excerpt": str(source.get("excerpt"))[:500]}
                   for source in source_items if isinstance(source, dict) and safe_url(source.get("url"))
                   and source["url"].rstrip("/") in allowed and source.get("excerpt")]
        verdict = item.get("verdict") if isinstance(item.get("verdict"), str) and item.get("verdict") in {"match", "mismatch", "unclear"} and sources else "unclear"
        result[key] = {"requirement": quote, "requirement_quote": quote, "required": not bool(re.match(r"(?:prefer|prioriti[sz]e|ideally|optional)\b", quote, re.I)),
                       "verdict": verdict, "reasoning": str(item.get("reasoning") or "")[:1500],
                       "value": str(item.get("value") or "Unclear"), "sources": sources[:3]}
    if contact is not None:
        check = evaluate_contact(contact, prompt)
        result["contact_method"] = {"requirement": check["requirement"], "verdict": check["verdict"], "required": True,
                                    "reasoning": "One permitted contact route is enough. Inbox delivery and call reachability are not guaranteed."}
    return result


def icp_assessment(checks):
    required = {key: item for key, item in checks.items()
                if key not in {"contact_method", "growth"} and item.get("required", item.get("verdict") != "not_specified")}
    mismatches = [key for key, item in required.items() if item.get("verdict") == "mismatch"]
    pending = [key for key, item in required.items() if item.get("verdict") not in {"match", "mismatch"}]
    score = sum(1.0 if item.get("verdict") == "match" else 0.5 if item.get("verdict") != "mismatch" else 0 for item in required.values())
    score = round(100.0 * score / len(required), 1) if required else 0.0
    return {"score": score, "match": bool(required) and not mismatches and not pending,
            "mismatches": mismatches, "pending": pending or ([] if required else ["ICP criteria unavailable"])}


def qualification(checks, contact, web, prompt=""):
    icp = icp_assessment(checks)
    contact_check = evaluate_contact(contact, prompt)
    growth = normalize_growth(web.get("growth_evidence"), web.get("sources", []), web.get("checked_at"))
    total = icp["score"] * WEIGHTS["icp"] + contact_check["score"] * WEIGHTS["contact"] + growth["score"] * WEIGHTS["growth"]
    weighted_score = round(total, 1)
    if icp["mismatches"] or contact_check["verdict"] == "mismatch":
        total = min(49.0, total)
        status = "uncontactable" if contact_check["verdict"] == "mismatch" else "disqualified"
    elif icp["match"] and contact_check["verdict"] == "match":
        status = "qualified"
    else:
        total = min(79.9, total)
        status = "needs_review"
    total = round(max(0.0, min(100.0, total)), 1)
    priority = priority_for(total)
    reasons = []
    if icp["mismatches"]:
        reasons.append("Confirmed ICP mismatches: " + ", ".join(FIELDS.get(key, checks.get(key, {}).get("requirement", key)) for key in icp["mismatches"]))
    if icp["pending"]:
        reasons.append("ICP evidence needs review: " + ", ".join(FIELDS.get(key, checks.get(key, {}).get("requirement", key)) for key in icp["pending"]))
    if contact_check["verdict"] != "match":
        reasons.append("No permitted contact route established" if contact_check["verdict"] == "mismatch" else "Contact format/availability needs review")
    if status in {"uncontactable", "disqualified"}:
        reasons.append("Qualification rule limits the final score to at most 49, regardless of company fit or growth")
    elif status == "needs_review":
        reasons.append("Unresolved required evidence limits the final score to at most 79.9 until reviewed")
    if status == "qualified":
        reasons.append("All required ICP checks match and at least one permitted contact route is available")
    facts = web.get("evidence") if isinstance(web.get("evidence"), dict) else {}
    confirmed = [f"{FIELDS[key]}: {str(facts[key].get('value'))[:180]}"
                 for key in ("employee_count", "annual_revenue", "industry", "seniority")
                 if isinstance(facts.get(key), dict) and facts[key].get("status") == "confirmed"]
    if confirmed:
        reasons.append("Researched facts — " + "; ".join(confirmed))
    reasons.append(f"Sourced growth ranking: {growth['score']}/100; missing growth evidence does not disqualify an otherwise qualified lead")
    reasons.append("Email inbox delivery, phone ownership and call reachability are unverified")
    return {"scoring_version": SCORING_VERSION, "qualification_status": status,
            "total_score": total, "priority": priority, "icp_score": icp["score"],
            "contact_score": contact_check["score"], "growth_score": growth["score"],
            "reasoning": ". ".join(reasons) + ".", "growth_signals": growth["value"],
            "details": {"weights": WEIGHTS, "icp": icp, "contact": contact_check,
                        "growth": growth, "reasons": reasons, "raw_weighted_score": weighted_score}}
