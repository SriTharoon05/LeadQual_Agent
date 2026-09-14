"""Remove unambiguous duplicate people within a file using the DB identity rules."""
import logging

import pandas as pd

from app.utils.lead_identity import KEY_FIELDS, identity_fields, words

logger = logging.getLogger(__name__)


def deduplicate(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    columns = {
        "CRM Contact ID": "crm_contact_id", "Email Address": "email_address",
        "LinkedIn Profile URL": "linkedin_profile_url", "Contact Name": "contact_name",
        "Company Name": "company_name",
    }
    index = {}
    identities = []
    kept = []
    for position, row in enumerate(df.to_dict("records")):
        keys = identity_fields({field: row.get(column) for column, field in columns.items()})
        candidates = set()
        for key, value in keys.items():
            if value:
                candidates.update(index.get((key, value), set()))
        valid = [i for i in candidates if not any(
            keys[key] and identities[i][key] and keys[key] != identities[i][key]
            for key in KEY_FIELDS[:3])]
        if len(valid) == 1:
            identity = valid[0]
            for key, value in keys.items():
                if value and not identities[identity][key]:
                    identities[identity][key] = value
        else:
            identity = len(identities)
            identities.append(keys.copy())
            kept.append(position)
        for key, value in keys.items():
            if value:
                index.setdefault((key, value), set()).add(identity)
    clean = df.iloc[kept].reset_index(drop=True)
    stats = {"original": len(df), "duplicates_removed": len(df) - len(clean), "final": len(clean)}
    logger.info("File deduplication: %s", stats)
    return clean, stats


def _normalise(value: str) -> str:
    return words(value)
