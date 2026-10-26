"""Local, fail-closed privacy boundary for every model request.

Known identities live in request-local context only. They are never logged or
restored into provider messages. No network-backed detector is used.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import unicodedata
import uuid
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from types import SimpleNamespace

from app.errors import AIPrivacyError

MASK = '[REDACTED]'
logger = logging.getLogger('ai_box.security.privacy')
_identities = ContextVar('ai_privacy_identities', default=frozenset())
_audit_id = ContextVar('ai_privacy_audit_id', default=None)
_lock = threading.Lock()
MAX_TEXT = 250_000
MAX_DEPTH = 32


def _key(value):
    return re.sub(r'[^a-z0-9]', '', unicodedata.normalize('NFKC', str(value)).casefold())


_SENSITIVE = re.compile(
    r'^(?:name|fullname|firstname|lastname|middlename|displayname|legalname|businessname|tradingname|companyname|customerid|sellerid|userid|'
    r'seller|merchant|owner|beneficiary|proprietor|company|'
    r'(?:seller|owner|customer|consignee|buyer|merchant|beneficiary|account(?:holder)?|contact|director|proprietor|father|mother|spouse)'
    r'(?:name|fullname|firstname|lastname|id)|'
    r'.*(?:email|mobile|phone|telephone|whatsapp|aadhaar|aadhar|pancard|pannumber|panid|passport|'
    r'voterid|drivinglicen[cs]e|bankaccount|accountnumber|ifsc|gstin|gstnumber|taxid|kyc|'
    r'identitydocument|idproof|addressproof|signature|selfie|dob|dateofbirth|password|secret|accesstoken|apikey|authorization|panno|gstno|upiid).*|'
    r'.*(?:address|addressline)[0-9]*|'
    r'(?:pan|gst|iban|swift|address|street|streetaddress|shippingaddress|billingaddress|residentialaddress))$')
# Generic numeric identifiers, credentials and KYC documents have no analytical use.
_PATTERNS = (
    ('email', re.compile(r'(?<![\w.+-])[\w.!#$%&\x27*+/=?^`{|}~-]+@[\w-]+(?:\.[\w-]+)+', re.I)),
    ('upi', re.compile(r'(?<![\w.+-])[\w.+-]+@[a-z][a-z0-9.-]*(?!\w)', re.I)),
    ('email_obfuscated', re.compile(r'\b[\w.+-]+\s*(?:\[at\]|\(at\)| at )\s*[\w-]+\s*(?:\[dot\]|\(dot\)| dot )\s*[a-z]{2,}\b', re.I)),
    ('pan', re.compile(r'(?<![a-z0-9])[a-z]{5}[ -]?[0-9]{4}[ -]?[a-z](?![a-z0-9])', re.I)),
    ('gst', re.compile(r'(?<![a-z0-9])[0-9]{2}[a-z]{5}[0-9]{4}[a-z][a-z0-9]z[a-z0-9](?![a-z0-9])', re.I)),
    ('ifsc', re.compile(r'(?<![a-z0-9])[a-z]{4}0[a-z0-9]{6}(?![a-z0-9])', re.I)),
    ('iban', re.compile(r'(?<![a-z0-9])[a-z]{2}[0-9]{2}(?: ?[a-z0-9]){10,30}(?![a-z0-9])', re.I)),
    ('passport', re.compile(r'(?<![a-z0-9])[a-z][0-9]{7}(?![a-z0-9])', re.I)),
    ('long_identifier', re.compile(r'(?<!\w)(?:\+\s*)?(?:[0-9][\s().-]*){9,19}(?!\w)')),
    ('credential', re.compile(r'\bBearer\s+[a-z0-9._~+/=-]+|\bsk-[a-z0-9_-]{8,}', re.I)),
    ('url', re.compile(r'https?://[^\s<>"\x27]+|data:[^\s<>"\x27]+', re.I)),
    ('street_address', re.compile(r'\b[0-9]+\s+(?:[\w.-]+\s+){0,5}(?:street|road|lane|avenue|nagar|colony)\b[^\n;<>]*', re.I)),
)
_LABEL = re.compile(
    r'(?im)\b(?:(?:seller|owner|customer|merchant|beneficiary|account holder|father|mother|spouse|company)\s+name|'
    r'my name|full name|email|mobile|phone|aadhaar|aadhar|pan(?: card| number| no\.?)?|passport|'
    r'bank account|account number|ifsc|gstin|kyc|date of birth|dob|address|seller id)'
    r'\s*(?:is\s+|[:=]\s*)([^\n;|<>]+)')
_HINDI_LABEL = re.compile(r'(?:मेरा नाम|विक्रेता का नाम|नाम|पता|आधार(?: नंबर)?|पैन(?: नंबर)?)\s*[:=]?\s+[^\n;|<>]+')
# Encoded/opaque files cannot be scanned as text. They must never reach a text model.
_OPAQUE = re.compile(r'(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{160,}={0,2}(?![A-Za-z0-9+/])')
_UNINSPECTABLE = re.compile(r'\bbase64\b|-----BEGIN |%[0-9a-f]{2}|\b0x[0-9a-f]{24,}', re.I)
_CONTROL = re.compile(r'[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]')
# Fixed public reporting vocabulary, never caller-supplied exemptions. Exact
# known seller identities are still masked before local entity recognition.
_PUBLIC_TERMS = frozenset(word.casefold() for word in (
    'JSON', 'SQL', 'UI', 'ISO', 'YYYY-MM-DD', 'COD', 'Prepaid', 'RTO', 'NDR', 'OFD',
    'KPI', 'PDF', 'DOCX', 'PPTX', 'AWB', 'LRN', 'PIN', 'PAN', 'GST', 'KYC',
    'Delivered', 'Undelivered', 'In Transit', 'in_transit', 'Pending', 'Cancelled',
    'Shipment', 'shipments', 'English', 'Hindi', 'Hinglish', 'Devanagari', 'NEVER'))
# Statistical name detection is weak for Devanagari. Permit a fixed reporting
# vocabulary; unknown words are ambiguous and block instead of being sent raw.
_HINDI_REPORT_WORDS = frozenset(('शिपमेंट शिपमेंट्स रिपोर्ट रिपोर्ट्स बनाओ बनाएं बनाइए बनाना करो करें '
    'दिखाओ दिखाएं दिखाइए दिखाना दो दें बताओ बताएं विश्लेषण जानकारी विवरण सारांश चार्ट ग्राफ '
    'तालिका टेबल डिलीवरी डिलीवर डिलीवर्ड रिटर्न आरटीओ सीओडी पिन पिनकोड पिनकोडों '
    'कूरियर कुरियर ऑर्डर ऑर्डर्स शिपिंग शुल्क चार्ज चार्जेस लागत कुल औसत संख्या प्रतिशत '
    'दर स्थिति स्टेटस लंबित रद्द सफल असफल वितरण ट्रेंड तुलना प्रदर्शन पिछले पिछली दिनों '
    'दिन महीने महीना सप्ताह साल आज कल अभी सभी सारे केवल सिर्फ मेरे मेरी मुझे हमारा '
    'हमारे की का के को में से तक पर और या साथ बिना लिए लिये नहीं हटाओ जोड़ो जोड़ें '
    'इस उस एक हैं है हुए हुआ हुई सबसे ज्यादा कम अधिक अनुसार आधार तैयार कर कृपया '
    'शहर शहरों राज्य राज्यों दिल्ली मुंबई बेंगलुरु चेन्नई हैदराबाद पुणे अहमदाबाद '
    'भुगतान प्रीपेड कैश डिलीवरी ट्रांजिट इनसाइट इनसाइट्स').split())


@lru_cache(maxsize=1)
def _name_detector():
    import spacy
    english = spacy.load('en_core_web_sm', disable=['tagger', 'parser', 'lemmatizer', 'attribute_ruler'])
    multilingual = spacy.load('xx_ent_wiki_sm')
    if any('ner' not in model.pipe_names for model in (english, multilingual)):
        raise RuntimeError('Missing local name detector')
    return _LocalNames((english, multilingual))


class _LocalNames:
    def __init__(self, models):
        self.models = models

    def __call__(self, text):
        # People can be labelled ORG (e.g. an individual trading under their own
        # name). Conservatively redact both persons and organizations.
        models = self.models if any(ord(c) > 127 and c.isalpha() for c in text) else self.models[:1]
        spans = sorted((entity.start_char, entity.end_char) for model in models
                       for entity in model(text).ents if (entity.label_ in {'PERSON', 'PER'} or
                           (entity.label_ == 'ORG' and len(entity.text.split()) > 1))
                       and entity.text.casefold().strip() not in _PUBLIC_TERMS)
        merged = []
        for start, end in spans:
            if merged and start < merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        return SimpleNamespace(ents=[SimpleNamespace(start_char=start, end_char=end,
            text=text[start:end], label_='PERSON') for start, end in merged])


def _normalize(text):
    text = _CONTROL.sub('', unicodedata.normalize('NFKC', text))
    return ''.join(str(unicodedata.digit(c)) if c.isdigit() else c for c in text)


def _json_parts(text):
    """Decode embedded JSON, including JSON inside XML prompt delimiters."""
    decoder, pos = json.JSONDecoder(), 0
    while pos < len(text):
        match = re.search(r'[\[{]', text[pos:])
        if not match:
            return
        start = pos + match.start()
        try:
            value, end = decoder.raw_decode(text, start)
        except ValueError:
            pos = start + 1
            continue
        if isinstance(value, (dict, list)):
            yield start, end, value
        pos = end


def _collect(value, found, depth=0, sensitive=False):
    if depth > MAX_DEPTH:
        raise ValueError('Privacy context nesting limit')
    if isinstance(value, dict):
        for key, item in value.items():
            _collect(item, found, depth+1, sensitive or _sensitive_key(key, value))
    elif isinstance(value, list):
        for item in value:
            _collect(item, found, depth+1, sensitive)
    elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
        text = _normalize(str(value)).strip()
        if sensitive and text and text != MASK:
            found.add(text)
        elif isinstance(value, str):
            for _, _, item in _json_parts(text):
                _collect(item, found, depth+1)


@contextmanager
def privacy_context(*values):
    try:
        found = set(_identities.get())
        for value in values:
            _collect(value, found)
        token = _identities.set(frozenset(found))
        audit_token = _audit_id.set(_audit_id.get() or uuid.uuid4().hex)
    except Exception:
        _event('blocked', 'context_failed', {})
        raise AIPrivacyError('The privacy check could not safely inspect this request.') from None
    try:
        yield
    finally:
        _identities.reset(token)
        _audit_id.reset(audit_token)


def register_source_identities(value):
    """Called on backend-fetched records before constructing model prompts."""
    try:
        found = set(_identities.get())
        _collect(value, found)
        _identities.set(frozenset(found))
    except Exception:
        _event('blocked', 'context_failed', {})
        raise AIPrivacyError('The privacy check could not safely inspect the source data.') from None


def _event(action, reason, counts):
    # Never log content, exception messages, field paths, IDs, hashes or matched values.
    logger.warning('%s', json.dumps({'event': 'ai_privacy', 'action': action,
                                    'audit_id': _audit_id.get() or uuid.uuid4().hex,
                                    'reason': reason, 'categories': dict(+Counter(counts))}, sort_keys=True))


def _sensitive_key(key, owner):
    # Registered operation metadata uses "name" for metrics, not a person's name.
    if _key(key) == 'name' and any(k in owner for k in ('agg', 'kind', 'aliases')):
        from app.charts.fields import get_adapter
        adapter = get_adapter('shipment')
        if isinstance(owner.get('name'), str) and owner['name'] in (adapter.fields.keys() | adapter.measures.keys()):
            return False
    return bool(_SENSITIVE.fullmatch(_key(key)))


def _text_fragments(value, depth=0):
    """Scan JSON strings in their own context, not across unrelated schema fields."""
    if depth > MAX_DEPTH:
        raise ValueError('Privacy scan limit')
    if isinstance(value, dict):
        for key, item in value.items():
            if ' ' in key:
                yield key
            yield from _text_fragments(item, depth+1)
    elif isinstance(value, list):
        for item in value:
            yield from _text_fragments(item, depth+1)
    elif isinstance(value, str):
        pos = 0
        parts = list(_json_parts(value))
        for start, end, item in parts:
            yield value[pos:start]
            yield from _text_fragments(item, depth+1)
            pos = end
        yield value[pos:]


class PrivacyGuard:
    def protect(self, messages):
        counts = Counter()
        try:
            if not isinstance(messages, list) or not messages:
                raise ValueError('Invalid messages')
            if sum(len(m.get('content', '')) for m in messages if isinstance(m, dict) and isinstance(m.get('content'), str)) > MAX_TEXT:
                raise ValueError('Privacy input limit')
            found = set(_identities.get())
            _collect(messages, found)
            # A model must be installed locally. There is no regex-only fallback.
            with _lock:
                detector = _name_detector()
                protected = []
                for message in messages:
                    if (not isinstance(message, dict) or set(message) != {'role', 'content'} or
                            message['role'] not in {'system', 'user', 'assistant'} or not isinstance(message['content'], str)):
                        raise ValueError('Unsupported model input')
                    protected.append({'role': message['role'], 'content': self._text(message['content'], found, counts, detector, 0)})
            # Independently re-scan known identities and deterministic patterns before dispatch.
            for message in protected:
                text = message['content']
                if any(pattern.search(text) for _, pattern in _PATTERNS) or _OPAQUE.search(text):
                    raise ValueError('Residual sensitive content')
                for identity in found:
                    if identity != MASK and self._identity_pattern(identity).search(text):
                        raise ValueError('Residual identity')
                with _lock:
                    # A second local pass may discover names whose context changed
                    # after redaction. Ambiguity blocks dispatch, never relaxes it.
                    if any(entity.label_ in {'PERSON', 'PER'} and MASK not in entity.text
                           for fragment in _text_fragments(text) if fragment.strip() and fragment != MASK
                           for entity in detector(fragment).ents):
                        raise ValueError('Residual person')
        except Exception:
            _event('blocked', 'inspection_failed', counts)
            raise AIPrivacyError('The request was blocked because its data could not be safely protected for AI processing.') from None
        _event('sanitized' if +counts else 'allowed', 'checked', counts)
        return protected

    @staticmethod
    def _identity_pattern(identity):
        return re.compile(r'(?<!\w)' + re.escape(identity) + r'(?!\w)', re.I)

    def _text(self, text, identities, counts, detector, depth):
        if depth > MAX_DEPTH or len(text) > MAX_TEXT:
            raise ValueError('Privacy scan limit')
        text = _normalize(text)
        if _OPAQUE.search(text) or _UNINSPECTABLE.search(text):
            raise ValueError('Uninspectable encoded input')
        parts = list(_json_parts(text))
        if parts:
            output, pos = [], 0
            for start, end, item in parts:
                output.append(self._plain(text[pos:start], identities, counts, detector))
                output.append(json.dumps(self._value(item, identities, counts, detector, depth+1), ensure_ascii=False))
                pos = end
            output.append(self._plain(text[pos:], identities, counts, detector))
            return ''.join(output)
        return self._plain(text, identities, counts, detector)

    def _value(self, value, identities, counts, detector, depth):
        if depth > MAX_DEPTH:
            raise ValueError('Privacy scan limit')
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                safe_key = self._plain(key, identities, counts, detector, names=' ' in key)
                if _sensitive_key(key, value):
                    counts['sensitive_field'] += 1
                    result[safe_key] = MASK
                else:
                    if safe_key in result:
                        raise ValueError('Redacted key collision')
                    result[safe_key] = self._value(item, identities, counts, detector, depth+1)
            return result
        if isinstance(value, list):
            return [self._value(v, identities, counts, detector, depth+1) for v in value]
        if isinstance(value, str):
            return self._text(value, identities, counts, detector, depth+1)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            text = str(value)
            if text in identities or any(p.search(text) for _, p in _PATTERNS):
                counts['numeric_identifier'] += 1
                return MASK
        return value

    def _plain(self, text, identities, counts, detector, names=True):
        for identity in sorted(identities, key=len, reverse=True):
            text, count = self._identity_pattern(identity).subn(MASK, text)
            counts['known_identity'] += count
        for label, pattern in _PATTERNS:
            text, count = pattern.subn(MASK, text)
            counts[label] += count
        for pattern in (_LABEL, _HINDI_LABEL):
            text, count = pattern.subn(MASK, text)
            counts['labelled_identity'] += count
        for word in re.findall(r'[\u0900-\u097f]+', text):
            word = ''.join(c for c in word if unicodedata.category(c)[0] in {'L', 'M'})
            if word and word not in _HINDI_REPORT_WORDS:
                raise ValueError('Uncertain non-Latin identity')
        if any(c.isalpha() and 'LATIN' not in unicodedata.name(c, '') and not '\u0900' <= c <= '\u097f' for c in text):
            raise ValueError('Unsupported privacy language')
        if names and text.strip():
            entities = detector(text).ents
            for entity in reversed(entities):
                if entity.label_ in {'PERSON', 'PER'} and MASK not in entity.text:
                    text = text[:entity.start_char] + MASK + text[entity.end_char:]
                    counts['person'] += 1
        return text


guard = PrivacyGuard()
