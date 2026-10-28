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
_MASK_ROUNDS = 3


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
# vocabulary; unknown words are ambiguous and are masked instead of being sent raw.
_HINDI_REPORT_WORDS = frozenset(('शिपमेंट शिपमेंट्स रिपोर्ट रिपोर्ट्स बनाओ बनाएं बनाइए बनाना करो करें '
    'दिखाओ दिखाएं दिखाइए दिखाना दो दें बताओ बताएं विश्लेषण जानकारी विवरण सारांश चार्ट ग्राफ '
    'तालिका टेबल डिलीवरी डिलीवर डिलीवर्ड रिटर्न आरटीओ सीओडी पिन पिनकोड पिनकोडों '
    'कूरियर कुरियर ऑर्डर ऑर्डर्स शिपिंग शुल्क चार्ज चार्जेस लागत कुल औसत संख्या प्रतिशत '
    'दर स्थिति स्टेटस लंबित रद्द सफल असफल वितरण ट्रेंड तुलना प्रदर्शन पिछले पिछली दिनों '
    'दिन महीने महीना सप्ताह साल आज कल अभी सभी सारे केवल सिर्फ मेरे मेरी मुझे हमारा '
    'हमारे की का के को में से तक पर और या साथ बिना लिए लिये नहीं हटाओ जोड़ो जोड़ें '
    'इस उस एक हैं है हुए हुआ हुई सबसे ज्यादा कम अधिक अनुसार आधार तैयार कर कृपया '
    'शहर शहरों राज्य राज्यों दिल्ली मुंबई बेंगलुरु चेन्नई हैदराबाद पुणे अहमदाबाद '
    'भुगतान प्रीपेड कैश डिलीवरी ट्रांजिट इनसाइट इनसाइट्स '
    # Period, comparison and section-title words, including those the planner writes in Hindi titles.
    'दैनिक साप्ताहिक मासिक वार्षिक विभाजन तुलनात्मक अवलोकन रुझान प्रवृत्ति गिनती हिस्सा अनुपात '
    'समय अवधि तारीख तिथि दिनांक महीनों महीनो माह हफ्ते हफ़्ते हफ्ता हफ़्ता हफ्तों हफ़्तों सप्ताहों सालों '
    'वर्ष वर्षों आखिरी आख़िरी अंतिम पिछला बीते गत दौरान बनाम अंतर बदलाव वृद्धि गिरावट शीर्ष टॉप '
    'कूरियरों राज्यवार शहरवार कूरियरवार स्थितिवार मोड प्रकार श्रेणी कारण कारणों सुझाव सिफारिश सिफारिशें '
    'निष्कर्ष परिणाम आंकड़े आँकड़े डेटा डाटा पेंडिंग कैंसल वापसी वापस पहुंचे पहुँचे रास्ते ट्रांज़िट '
    'डिलीवरीज़ डिलीवर्ड ऑर्डरों शिपमेंटों वाले वाली वाला गया गई गए था थी थे प्रति दरें दो तीन चार पांच पाँच '
    'छह छः सात आठ नौ दस ग्यारह बारह तीस साठ नब्बे').split())
_HINDI_REPORT_WORDS = frozenset(unicodedata.normalize('NFKC', word) for word in _HINDI_REPORT_WORDS)
# Words of the backend's own report vocabulary (schema labels, metric names, statuses, JSON schema
# keywords). The local name model tags phrases such as "Delivery Rate" or "Manifest Date" as people;
# a span made only of these words is not a person. Any other word in the span keeps it detected.
_REPORT_VOCABULARY_WORDS = (
    'rate rates count counts share trend trends daily weekly monthly yearly vs versus comparison compare compared '
    'breakdown summary total totals average avg median top bottom highest lowest order orders shipment shipments '
    'delivered delivery deliveries transit rto status statuses date dates day days week weeks month months year years '
    'today performance overview analysis insight insights report reports section sections metric metrics id ids kpi kpis '
    'volume pending cancelled canceled cancellation cancellations returned return returns exception exceptions ndr '
    'lost undelivered picked pickup pickups scheduled not manifest manifested courier couriers carrier payment mode '
    'modes cod prepaid state states city cities zone zones pincode pincodes pin weight charge charges shipping amount '
    'value values revenue cost freight time timeline stage stages reason reasons distribution mix ratio percent '
    'percentage percentages chart charts table tables graph bar line pie doughnut area histogram scatter limitation '
    'limitations note notes narrative recommendation recommendations action actions requested request requirement '
    'requirements kind schema type types string integer number boolean null array object gte gt lte lt eq ne between '
    'min max ranking rank data missing required field fields available unavailable consignee buyer buyers first '
    'last previous current period range selected scope source verified collectable invoice weekday out for of and '
    'or by the in to per vs ai box iso yyyy mm dd fy quarter quarterly trip attempt attempts remarks remark '
    'title description properties enum items default ref defs anyof allof oneof format').split()


@lru_cache(maxsize=1)
def _report_vocabulary():
    words = set(_REPORT_VOCABULARY_WORDS) | {w for term in _PUBLIC_TERMS for w in re.findall(r'[^\W\d_]+', term)}
    try:
        from app.charts.fields import get_adapter
        from app.analysis.vocabulary import get_vocabulary
        adapter = get_adapter('shipment')
        texts = [*adapter.fields, *(f.label for f in adapter.fields.values()), *adapter.measures,
                 *(m.label for m in adapter.measures.values()), *get_vocabulary('shipment').status_values]
    except Exception:  # The fixed list above still applies; detection is never relaxed further.
        texts = []
    for text in texts:
        words.update(re.findall(r'[^\W\d_]+', str(text).casefold()))
    return frozenset(words)


def _report_vocabulary_only(text):
    words = re.findall(r'[^\W\d_]+', text.casefold())
    return bool(words) and all(word in _report_vocabulary() for word in words)


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
                       and entity.text.casefold().strip() not in _PUBLIC_TERMS
                       and not _report_vocabulary_only(entity.text))
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
    except Exception as exc:
        _event('blocked', 'context_failed', {}, failure_code(exc))
        raise _blocked(failure_code(exc)) from None
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
    except Exception as exc:
        _event('blocked', 'context_failed', {}, failure_code(exc))
        raise _blocked(failure_code(exc)) from None


PUBLIC_MESSAGE = "We couldn't prepare your report from this request. Please rephrase it and try again."
# Fixed internal failure codes (from this module's own ValueError texts); safe to log, never shown to users.
_FAILURE_CODES = {
    'Invalid messages': 'invalid_messages', 'Privacy input limit': 'input_limit',
    'Unsupported model input': 'unsupported_input', 'Residual sensitive content': 'residual_pattern',
    'Residual identity': 'residual_identity', 'Residual person': 'residual_person',
    'Privacy scan limit': 'scan_limit', 'Uninspectable encoded input': 'uninspectable_input',
    'Redacted key collision': 'key_collision', 'Uncertain non-Latin identity': 'uncertain_devanagari_word',
    'Unsupported privacy language': 'unsupported_language', 'Privacy context nesting limit': 'nesting_limit',
}


def failure_code(exc):
    if type(exc) is ValueError and str(exc) in _FAILURE_CODES:
        return _FAILURE_CODES[str(exc)]
    return 'detector_error' if isinstance(exc, (RuntimeError, ImportError, OSError)) else 'internal_error'


def _blocked(code, stage=None):
    error = AIPrivacyError(PUBLIC_MESSAGE)
    error.internal_code = code  # For logs and the audit trail only; never part of the API response.
    error.stage = stage
    return error


def _event(action, reason, counts, detail=None, stage=None):
    # Never log content, exception messages, field paths, IDs, hashes or matched values.
    event = {'event': 'ai_privacy', 'action': action, 'audit_id': _audit_id.get() or uuid.uuid4().hex,
             'reason': reason, 'categories': dict(+Counter(counts))}
    if detail:
        event['detail'] = detail
    if stage:
        event['stage'] = stage
    logger.warning('%s', json.dumps(event, sort_keys=True))


def _stage(messages):
    # The fixed backend tag that opens each system prompt, e.g. "[report_plan]"; never message content.
    first = messages[0] if isinstance(messages, list) and messages and isinstance(messages[0], dict) else {}
    match = re.match(r'\[([a-z_]{1,40})\]', first.get('content') or '') if first.get('role') == 'system' else None
    return match[1] if match else None


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
        except Exception as exc:
            code, stage = failure_code(exc), _stage(messages)
            _event('blocked', 'inspection_failed', counts, code, stage)
            raise _blocked(code, stage) from None
        _event('sanitized' if +counts else 'allowed', 'checked', counts, stage=_stage(messages))
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
        # Name detection is unreliable for Devanagari: every word outside the fixed reporting vocabulary is
        # masked (never sent), so an unrecognised word cannot leak and cannot stop a valid report either.
        def devanagari(match):
            word = ''.join(c for c in match.group(0) if unicodedata.category(c)[0] in {'L', 'M'})
            if word and word not in _HINDI_REPORT_WORDS:
                counts['uncertain_devanagari_word'] += 1
                return MASK
            return match.group(0)
        text = re.sub(r'[\u0900-\u097f]+', devanagari, text)
        if any(c.isalpha() and 'LATIN' not in unicodedata.name(c, '') and not '\u0900' <= c <= '\u097f' for c in text):
            raise ValueError('Unsupported privacy language')
        if names and text.strip():
            # Masking one span changes the context of the rest, and the model can then tag a neighbour
            # (e.g. "[REDACTED] vs RTO Rate"). Mask again until nothing person-like remains; text that is
            # still unstable after a few rounds blocks. The final independent re-scan in protect() is unchanged.
            for _ in range(_MASK_ROUNDS):
                entities = [e for e in detector(text).ents if e.label_ in {'PERSON', 'PER'} and MASK not in e.text]
                if not entities:
                    break
                for entity in reversed(entities):
                    text = text[:entity.start_char] + MASK + text[entity.end_char:]
                    counts['person'] += 1
            else:
                if any(e.label_ in {'PERSON', 'PER'} and MASK not in e.text for e in detector(text).ents):
                    raise ValueError('Residual person')
        return text


guard = PrivacyGuard()
