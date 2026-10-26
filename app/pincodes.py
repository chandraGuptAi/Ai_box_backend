"""Postal identifiers are categories, never numeric measures."""
import re


def normalize_pincode(value):
    """Accept source strings/integers; reject malformed Indian PINs without guessing."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        value = int(value)
    text = str(value).strip()
    return text if re.fullmatch(r'[1-9][0-9]{5}', text) else None


def normalize_pincodes(value):
    """Validate UI input and preserve stable, unique string values."""
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    result = []
    for item in values:
        pin = normalize_pincode(item)
        if pin is None:
            raise ValueError('Pincodes must be six-digit Indian PIN codes')
        if pin not in result:
            result.append(pin)
    if len(result) > 100:
        raise ValueError('Select at most 100 pincodes')
    return result
