"""Recognised Indian states and union territories for map charts (names match common GeoJSON)."""
from __future__ import annotations

import re

STATES = (
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh", "Goa", "Gujarat", "Haryana",
    "Himachal Pradesh", "Jharkhand", "Karnataka", "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur", "Meghalaya",
    "Mizoram", "Nagaland", "Odisha", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu", "Telangana", "Tripura",
    "Uttar Pradesh", "Uttarakhand", "West Bengal",
    "Andaman and Nicobar Islands", "Chandigarh", "Dadra and Nagar Haveli and Daman and Diu", "Delhi",
    "Jammu and Kashmir", "Ladakh", "Lakshadweep", "Puducherry",
)

_ALIASES = {
    "ap": "Andhra Pradesh", "ar": "Arunachal Pradesh", "as": "Assam", "br": "Bihar", "cg": "Chhattisgarh",
    "ct": "Chhattisgarh", "ga": "Goa", "gj": "Gujarat", "hr": "Haryana", "hp": "Himachal Pradesh", "jh": "Jharkhand",
    "ka": "Karnataka", "kl": "Kerala", "mp": "Madhya Pradesh", "mh": "Maharashtra", "mn": "Manipur", "ml": "Meghalaya",
    "mz": "Mizoram", "nl": "Nagaland", "od": "Odisha", "or": "Odisha", "orissa": "Odisha", "pb": "Punjab",
    "rj": "Rajasthan", "sk": "Sikkim", "tn": "Tamil Nadu", "ts": "Telangana", "tg": "Telangana", "tr": "Tripura",
    "up": "Uttar Pradesh", "uk": "Uttarakhand", "ut": "Uttarakhand", "uttaranchal": "Uttarakhand", "wb": "West Bengal",
    "an": "Andaman and Nicobar Islands", "andaman nicobar": "Andaman and Nicobar Islands", "ch": "Chandigarh",
    "dn": "Dadra and Nagar Haveli and Daman and Diu", "dd": "Dadra and Nagar Haveli and Daman and Diu",
    "daman and diu": "Dadra and Nagar Haveli and Daman and Diu", "dadra and nagar haveli": "Dadra and Nagar Haveli and Daman and Diu",
    "dl": "Delhi", "new delhi": "Delhi", "nct of delhi": "Delhi", "delhi ncr": "Delhi", "jk": "Jammu and Kashmir",
    "j k": "Jammu and Kashmir", "la": "Ladakh", "ld": "Lakshadweep", "py": "Puducherry", "pondicherry": "Puducherry",
}
_LOOKUP = {**{s.lower(): s for s in STATES}, **_ALIASES}


def canonical_state(value: object) -> str | None:
    """Canonical state/UT name, or None when the value is not a recognisable Indian state."""
    text = re.sub(r"[^a-z ]+", " ", str(value or "").lower().replace("&", " and "))
    text = re.sub(r"\s+", " ", text).strip()
    return _LOOKUP.get(text) or _LOOKUP.get(text.replace(" islands", ""))
