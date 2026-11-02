"""Non-blocking PII filter for text sent to an AI model.

Personal data is replaced with [REDACTED] and every removal is recorded by category (never by
value), so the caller can tell the reader what was left out. Nothing here rejects a request:
text that cannot be classified is passed on and only the matched part of risky content is masked.

The same file lives in Ai_Box_Service (app/modules/privacy/engine.py, the primary filter) and in
AI_python_backend (app/security/privacy_engine.py, the fallback when that service is unreachable).
Keep the two copies identical.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import unicodedata

MASK = '[REDACTED]'
MAX_NER_TEXT = 250_000
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
_FIELD_GROUPS = (
    ('field_email', ('email',)),
    ('field_phone', ('mobile', 'phone', 'telephone', 'whatsapp')),
    ('field_credential', ('password', 'secret', 'accesstoken', 'apikey', 'authorization')),
    ('field_bank', ('bank', 'accountnumber', 'ifsc', 'iban', 'swift', 'upi')),
    ('field_id', ('aadhaar', 'aadhar', 'pan', 'passport', 'voter', 'driving', 'gst', 'taxid', 'kyc', 'identity',
                  'proof', 'signature', 'selfie', 'dob', 'dateofbirth')),
    ('field_address', ('address', 'street')),
)
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
    ('street_address', re.compile(r'\b[0-9]+\s+(?:[\w.-]+\s+){0,5}(?:street|road|avenue|nagar|colony)\b[^\n;<>]*', re.I)),
    # Encoded or opaque content cannot be read as text; only that part is masked.
    ('encoded', re.compile(r'(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{160,}={0,2}(?![A-Za-z0-9+/])|'
                           r'\S*%[0-9a-f]{2}\S*|\b0x[0-9a-f]{24,}\b', re.I)),
)
_CONTROL = re.compile(r'[​-‏‪-‮⁠-⁯﻿]')

# A word in Latin or Devanagari script. Python's \w does not cover Devanagari vowel signs.
_W = r"[A-Za-zÀ-ɏऀ-ॣॱ-ॿ][A-Za-z0-9À-ɏऀ-ॣ०-ॿ.'-]*"
_EDGE_BEFORE = r"(?<![A-Za-z0-9À-ɏऀ-ॣ०-ॿ])"
_EDGE_AFTER = r"(?![A-Za-z0-9À-ɏऀ-ॣ०-ॿ])"
_NAME_LABEL = re.compile(
    rf"(?i){_EDGE_BEFORE}(?:(?:my|full|seller|owner|customer|consignee|buyer|merchant|beneficiary|father|mother|spouse|company)(?:'s)?\s+name"
    rf"\s*(?:is|:|=|-)?|account holder(?: name)?\s*(?:is|:|=)|name\s*(?:is|:|=)|"
    rf"(?:mera|meraa|customer ka|seller ka|grahak ka)\s+naam\s*(?:hai|:|=|-)?|naam\s*(?:hai|:|=)|"
    rf"(?:मेरा|विक्रेता का|ग्राहक का)\s+नाम\s*(?:है|:|=|-)?|नाम\s*(?::|=))"
    rf"[ \t]*({_W}(?:[ \t]+{_W}){{0,3}})")
_DETAIL_LABEL = re.compile(
    r'(?im)\b(?:email|mobile|phone|aadhaar|aadhar|pan(?: card| number| no\.?)?|passport|bank account|account number|'
    r'ifsc|gstin|kyc|date of birth|dob|address|seller id)\s*(?:is\s+|[:=]\s*)([^\n;|<>]+)'
    r'|(?:पता|आधार(?: नंबर)?|पैन(?: नंबर)?)\s*[:=]\s*([^\n;|<>]+)')
_HONORIFIC = re.compile(
    rf"(?i){_EDGE_BEFORE}(?:mr|mrs|ms|miss|dr|shri|shree|sri|smt|श्री|श्रीमती|सुश्री|डॉ)\.?[ \t]+({_W}(?:[ \t]+{_W})?)")
_SURNAMES = (
    'kumar singh sharma verma gupta yadav patel shah jain agarwal aggarwal agrawal mishra pandey tiwari chauhan reddy '
    'nair iyer menon pillai ansari sheikh mehta joshi bhatt chaudhary choudhary thakur saxena srivastava '
    'malhotra kapoor khanna arora bansal goyal mittal sinha dubey shukla tripathi rajput rathore naidu mukherjee '
    'banerjee chatterjee ghosh dutta desai kulkarni deshmukh patil pawar shinde jadhav sandhu dhillon garg '
    'कुमार सिंह शर्मा वर्मा गुप्ता यादव पटेल शाह जैन अग्रवाल मिश्रा पांडे पाण्डेय तिवारी चौहान रेड्डी नायर अंसारी '
    'मेहता जोशी भट्ट चौधरी ठाकुर सक्सेना श्रीवास्तव मल्होत्रा कपूर खन्ना अरोड़ा बंसल गोयल मित्तल सिन्हा दुबे '
    'शुक्ला त्रिपाठी राजपूत राठौर देसाई कुलकर्णी देशमुख पाटिल पवार गर्ग').split()
_SURNAME = re.compile(
    rf"(?i){_EDGE_BEFORE}(?:({_W})[ \t]+)?((?:{'|'.join(map(re.escape, _SURNAMES))})){_EDGE_AFTER}")

# Never treated as a name. This list only prevents false masking; it never blocks anything.
_SAFE_WORDS = frozenset(word.casefold() for word in (
    # Reporting vocabulary (English)
    'json sql ui iso cod prepaid rto ndr ofd kpi pdf docx pptx xlsx awb lrn pin pincode pincodes gst kyc '
    'delivered undelivered transit in_transit pending cancelled canceled shipped processing unshipped fulfilled '
    'unfulfilled partially paid refunded returned lost damaged manifested picked pickup booked '
    'shipment shipments order orders report reports status statuses breakdown count counts percentage percent '
    'total average rate ratio trend trends compare comparison performance analysis analyse analyze summary '
    'chart charts graph table list top bottom best worst daily weekly monthly yearly day days week weeks month '
    'months year years today yesterday last next this previous current selected date range period zone '
    'courier couriers carrier delivery deliveries return returns charge charges cost freight weight revenue '
    'city cities state states region zone channel channels store payment mode value wise each every all '
    'show give create make generate add remove include display please with without for from by of and or the '
    'english hindi hinglish devanagari never insight insights dashboard kpis metric metrics '
    'january february march april may june july august september october november december '
    'monday tuesday wednesday thursday friday saturday sunday '
    # Hinglish
    'mujhe mera meri mere hamara hamare apna apne sabhi sab saare har kitne kitna kitni kaun kya kab kahan '
    'ka ki ke ko se me mein par aur ya bhi tak wala wale wali liye lie karo kare karein karna kardo do dena '
    'dikhao dikhaye dikhaiye dikhana batao bataye banao banaye banaiye chahiye hai hain tha the thi pichle '
    'pichhle pichla agle aaj kal abhi din hafte hafta mahine mahina saal hisaab hisab anusar wapas vapas '
    # Couriers, places and Hindi month names
    'delhivery bluedart blue dart ekart xpressbees dtdc ecom express shadowfax maruti amazon professional gati '
    'smartr india post aramex fedex dhl bigship shopify woocommerce '
    'delhi mumbai bengaluru bangalore chennai hyderabad pune ahmedabad kolkata jaipur lucknow surat kanpur '
    'nagpur indore bhopal patna noida gurgaon gurugram ghaziabad faridabad chandigarh '
    'maharashtra karnataka gujarat rajasthan punjab haryana bihar kerala odisha assam telangana uttar pradesh '
    'madhya bengal tamil nadu andhra jharkhand uttarakhand goa '
    # Hindi
    'शिपमेंट शिपमेंट्स रिपोर्ट रिपोर्ट्स बनाओ बनाएं बनाइए बनाना करो करें दिखाओ दिखाएं दिखाइए दिखाना दिखाए '
    'दो दें बताओ बताएं विश्लेषण जानकारी विवरण सारांश चार्ट ग्राफ तालिका टेबल डिलीवरी डिलीवर डिलीवर्ड रिटर्न '
    'आरटीओ सीओडी पिन पिनकोड पिनकोडों कूरियर कुरियर ऑर्डर ऑर्डर्स शिपिंग शुल्क चार्ज चार्जेस लागत कुल औसत '
    'संख्या प्रतिशत गिनती दर स्थिति स्टेटस लंबित रद्द सफल असफल वितरण ट्रेंड तुलना प्रदर्शन पिछले पिछली दिनों '
    'दिन हफ्ते हफ्ता सप्ताह महीने महीना साल आज कल अभी सभी सारे प्रत्येक हर केवल सिर्फ मेरे मेरी मुझे हमारा '
    'हमारे की का के को में से तक पर और या साथ बिना लिए लिये नहीं हटाओ जोड़ो जोड़ें इस उस एक हैं है हुए हुआ '
    'हुई सबसे ज्यादा कम अधिक अनुसार हिसाब आधार तैयार कर कृपया चयनित तिथि तारीख सीमा अवधि ब्रेकडाउन जिसमें '
    'जिनमें जाएं जाए कितने कितना कितनी वापस आए आया शहर शहरों राज्य राज्यों दिल्ली मुंबई बेंगलुरु चेन्नई '
    'हैदराबाद पुणे अहमदाबाद कोलकाता जयपुर लखनऊ भुगतान प्रीपेड कैश ट्रांजिट इनसाइट इनसाइट्स समय'
).split())
# A label or cue word ends the captured name here.
_STOP_WORDS = frozenset(
    'and or is was with for from of to in on by at aur ya hai hain ka ki ke ko se me mein wala wale wali ne '
    'और या है हैं का की के को से में वाले वाली ने'.split())


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


def collect_identities(value, found, depth=0, sensitive=False, field_names=frozenset()):
    """Add every value stored under a sensitive key (at any depth, including stringified JSON) to ``found``."""
    if depth > MAX_DEPTH:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            collect_identities(item, found, depth+1, sensitive or sensitive_key(key, value, field_names), field_names)
    elif isinstance(value, list):
        for item in value:
            collect_identities(item, found, depth+1, sensitive, field_names)
    elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
        text = _normalize(str(value)).strip()
        if sensitive and text and text != MASK:
            found.add(text)
        elif isinstance(value, str):
            for _, _, item in _json_parts(text):
                collect_identities(item, found, depth+1, field_names=field_names)


def sensitive_key(key, owner, field_names=frozenset()):
    # Registered operation metadata uses "name" for metrics, not a person's name.
    if (_key(key) == 'name' and any(k in owner for k in ('agg', 'kind', 'aliases'))
            and isinstance(owner.get('name'), str) and owner['name'] in field_names):
        return False
    return bool(_SENSITIVE.fullmatch(_key(key)))


def _field_category(key):
    compact = _key(key)
    for category, parts in _FIELD_GROUPS:
        # "pan" also occurs inside "company"; only a key starting with it is a PAN field.
        if any(compact.startswith(p) if p == 'pan' else p in compact for p in parts):
            return category
    return 'field_name'


def _words(text):
    return re.findall(_W, text)


_HONORIFIC_WORDS = frozenset('mr mrs ms miss dr shri shree sri smt श्री श्रीमती सुश्री डॉ'.split())


def _is_safe(text):
    # "Shree Maruti" is a courier: an honorific in front of safe words does not make a name.
    words = [w.casefold().strip(".'-") for w in _words(text)]
    words = [w for w in words if w not in _HONORIFIC_WORDS]
    return not words or all(word in _SAFE_WORDS for word in words)


def _trim_name(value):
    """The name part of a captured value: up to the first stop word, without trailing safe words."""
    words = []
    for word in value.split():
        if word.casefold() in _STOP_WORDS:
            break
        words.append(word)
    while words and words[-1].casefold().strip(".'-") in _SAFE_WORDS:
        words.pop()
    return ' '.join(words)


_lock = threading.Lock()


def load_detector():
    """Local spaCy name detector (English, plus the multilingual model for non-Latin text)."""
    import spacy
    english = spacy.load('en_core_web_sm', disable=['tagger', 'parser', 'lemmatizer', 'attribute_ruler'])
    multilingual = spacy.load('xx_ent_wiki_sm')
    if any('ner' not in model.pipe_names for model in (english, multilingual)):
        raise RuntimeError('Missing local name detector')

    def detect(text):
        models = (english, multilingual) if any(ord(c) > 127 and c.isalpha() for c in text) else (english,)
        with _lock:
            return [(e.start_char, e.end_char, e.label_, e.text) for model in models for e in model(text).ents]
    return detect


class Redactor:
    """Filters one batch of model messages. ``removed`` maps a value fingerprint to its category,
    so the same value found in several places or calls is counted once."""

    def __init__(self, identities=(), detector=None, field_names=frozenset()):
        self.identities = sorted({_normalize(str(i)).strip() for i in identities if str(i).strip()} - {MASK},
                                 key=len, reverse=True)
        self.detector = detector
        self.field_names = frozenset(field_names)
        self.removed = {}

    def messages(self, messages):
        if not isinstance(messages, list) or not messages:
            raise ValueError('Invalid messages')
        for message in messages:
            if (not isinstance(message, dict) or set(message) != {'role', 'content'} or
                    message['role'] not in {'system', 'user', 'assistant'} or not isinstance(message['content'], str)):
                raise ValueError('Unsupported model input')
        protected = [{'role': m['role'], 'content': self.text(m['content'])} for m in messages]
        # Re-scan the output: anything a later step uncovered is masked too.
        return [{'role': m['role'], 'content': self._deterministic(m['content'])} for m in protected]

    def text(self, text, depth=0):
        text = _normalize(text)
        if depth > MAX_DEPTH:
            return self._mask('encoded', text)
        parts = list(_json_parts(text))
        if not parts:
            return self._plain(text)
        output, pos = [], 0
        for start, end, item in parts:
            output.append(self._plain(text[pos:start]))
            output.append(json.dumps(self._value(item, depth+1), ensure_ascii=False))
            pos = end
        output.append(self._plain(text[pos:]))
        return ''.join(output)

    def _value(self, value, depth):
        if depth > MAX_DEPTH:
            return self._mask('encoded', json.dumps(value, default=str))
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                safe_key = self._plain(key, names=' ' in key)
                if sensitive_key(key, value, self.field_names):
                    if item != MASK:
                        self._record(_field_category(key), json.dumps(item, sort_keys=True, default=str))
                    result[safe_key] = MASK
                else:
                    result[safe_key if safe_key not in result else f'{safe_key}_{len(result)}'] = self._value(item, depth+1)
            return result
        if isinstance(value, list):
            return [self._value(v, depth+1) for v in value]
        if isinstance(value, str):
            return self.text(value, depth+1)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            text = str(value)
            if text in self.identities:
                return self._mask('known_identity', text)
            for category, pattern in _PATTERNS:
                if pattern.search(text):
                    return self._mask(category, text)
        return value

    def _plain(self, text, names=True):
        text = self._deterministic(text)
        text = _DETAIL_LABEL.sub(lambda m: self._mask_groups(m, 'labelled_detail'), text)
        text = _NAME_LABEL.sub(lambda m: self._mask_name(m, 1, 'labelled_name'), text)
        text = _HONORIFIC.sub(lambda m: self._mask_name(m, 1, 'honorific_name'), text)
        text = _SURNAME.sub(self._mask_surname, text)
        if names and self.detector and text.strip() and len(text) <= MAX_NER_TEXT:
            text = self._names(text)
        return text

    def _deterministic(self, text):
        for identity in self.identities:
            text = re.compile(r'(?<!\w)' + re.escape(identity) + r'(?!\w)', re.I).sub(
                lambda m: self._mask('known_identity', m[0]), text)
        for category, pattern in _PATTERNS:
            text = pattern.sub(lambda m, c=category: m[0] if m[0] == MASK else self._mask(c, m[0]), text)
        return text

    def _names(self, text):
        spans = []
        for start, end, label, value in self.detector(text):
            if not (label in {'PERSON', 'PER'} or (label == 'ORG' and len(value.split()) > 1)):
                continue
            if MASK in value or _is_safe(value):
                continue
            # Lower-case Latin spans are Hinglish words the English model mistakes for names.
            if re.search(r'[A-Za-z]', value) and not re.search(r'[A-Z]', value):
                continue
            spans.append((start, end))
        merged = []
        for start, end in sorted(spans):
            if merged and start < merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        for start, end in reversed(merged):
            text = text[:start] + self._mask('person', text[start:end]) + text[end:]
        return text

    def _mask_groups(self, match, category):
        text, base = match[0], match.start()
        for index in range(len(match.groups()), 0, -1):
            if match[index] and match[index].strip() != MASK:
                start, end = match.start(index) - base, match.end(index) - base
                text = text[:start] + self._mask(category, match[index]) + text[end:]
        return text

    def _mask_name(self, match, group, category):
        name = _trim_name(match[group])
        if not name or MASK in name or _is_safe(name):
            return match[0]
        start = match.start(group) - match.start()
        return match[0][:start] + self._mask(category, name) + match[0][start + len(name):]

    def _mask_surname(self, match):
        first = match[1]
        if first and (first.casefold() in _STOP_WORDS or first.casefold().strip(".'-") in _SAFE_WORDS or first == MASK):
            first = None
        start = match.start(1) if first else match.start(2)
        return match[0][:start - match.start()] + self._mask('surname', match.string[start:match.end(2)])

    def _mask(self, category, value):
        self._record(category, value)
        return MASK

    def _record(self, category, value):
        ref = hashlib.sha256(_normalize(str(value)).casefold().strip().encode()).hexdigest()[:16]
        self.removed.setdefault(ref, category)
