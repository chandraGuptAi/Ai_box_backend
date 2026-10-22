"""AWB candidate rules ported from the Node engine, with ASCII regex semantics."""

from __future__ import annotations

import re
from ..errors import EscalationError

PHONE = re.compile(r"^[6-9]\d{9}$", re.ASCII)
EXPLICIT = re.compile(
    r"\b(?:awb|lr|tracking(?:\s*(?:id|no|number))?)\s*(?:number|no\.?|id)?\s*[:#=-]?\s*$", re.I | re.ASCII
)
CONTACT = re.compile(
    r"(?:phone|mobile|contact|call|whatsapp|number pe)\s*(?:no\.?|number)?\s*[:#=-]?\s*$", re.I | re.ASCII
)


def extract_awbs(text: str, legacy: bool = False, min_digits: int = 8, max_digits: int = 16) -> list[dict]:
    if min_digits < 8 or max_digits > 16 or min_digits > max_digits:
        raise EscalationError(400, "Invalid AWB length configuration")
    text = str(text or "")
    values = {}
    for match in re.finditer(r"\b\d{8,16}\b", text, re.ASCII):
        awb = match.group()
        if not min_digits <= len(awb) <= max_digits:
            continue
        before = text[max(0, match.start() - 40) : match.start()]
        explicit = bool(EXPLICIT.search(before))
        phone = bool(PHONE.fullmatch(awb))
        if phone and (legacy or not explicit):
            continue
        if not legacy and CONTACT.search(before) and not explicit:
            continue
        if awb not in values or explicit:
            values[awb] = dict(
                awb=awb,
                confidence="explicit" if explicit else "candidate",
                phoneShaped=phone,
                rule=(
                    "Explicit AWB/LR/tracking label"
                    if explicit
                    else "8-16 digit numeric candidate; requires review"
                ),
            )
    return list(values.values())
