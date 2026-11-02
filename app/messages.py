"""Plain-language messages for sellers, in the language of their question.

Sellers are not developers: each message says what was looked for, what was found and what to try next.
Error codes and technical details stay in ``code``, ``details`` and the logs, never in this text.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
# Common Roman-script Hindi words; two or more mark a Hinglish question.
_HINGLISH = frozenset(
    "kitne kitna kitni kaun kaunsa kya kyu kyun kab kaha kahan mein mai mujhe mere meri mera hamare hume hamein "
    "ke ki ka ko se par pe aur bhi wala wale wali hue hua hui hai hain tha the thi banao bana banado banaiye "
    "dikhao dikhaiye batao bataiye chahiye karo kardo kijiye sabse zyada jyada kam pichhle pichle agle aaj kal "
    "hafte hafta mahine mahina saal din wapas lage laga lagi gaye gaya gayi".split())

_TEXT = {
    "no_shipments": {
        "en": "We didn't find any shipments{status} between {period}. Try selecting a longer date range{or_status}.",
        "hi": "{period} के बीच{status} कोई शिपमेंट नहीं मिला। कृपया लंबी तारीख़ सीमा चुनें{or_status}।",
        "hinglish": "{period} ke beech{status} koi shipment nahi mila. Lambi date range chunein{or_status}.",
    },
    "filter_no_match": {
        "en": "You have {count} {shipment_word}{status} between {period}, but none of them have {condition}. "
              "Check this, or remove it from your question to see all your shipments.",
        "hi": "{period} के बीच आपके {count} शिपमेंट{status} हैं, लेकिन इनमें से किसी में {condition} नहीं है। "
              "इसे जाँचें, या सभी शिपमेंट देखने के लिए इसे सवाल से हटा दें।",
        "hinglish": "{period} ke beech aapke {count} shipment{status} hain, lekin inmein se kisi mein {condition} nahi hai. "
                    "Ise check karein, ya sabhi shipments dekhne ke liye ise sawaal se hata dein.",
    },
    "order_not_found": {
        "en": "We couldn't find order {orders} among your shipments between {period}. It may have been created outside "
              "these dates. Try a longer date range.",
        "hi": "{period} के बीच आपके शिपमेंट में ऑर्डर {orders} नहीं मिला। हो सकता है यह इन तारीख़ों के बाहर बना हो। "
              "कृपया लंबी तारीख़ सीमा चुनें।",
        "hinglish": "{period} ke beech aapke shipments mein order {orders} nahi mila. Ho sakta hai yeh in dates ke bahar "
                    "bana ho. Lambi date range chunein.",
    },
    "fields_missing": {
        "en": "Your shipments between {period} don't include {fields}, so we can't build this report. "
              "You can ask about status, courier, city, payment mode or charges instead.",
        "hi": "{period} के बीच आपके शिपमेंट में {fields} की जानकारी नहीं है, इसलिए यह रिपोर्ट नहीं बन सकती। "
              "आप स्टेटस, कूरियर, शहर, पेमेंट मोड या चार्ज के बारे में पूछ सकते हैं।",
        "hinglish": "{period} ke beech aapke shipments mein {fields} ki jaankari nahi hai, isliye yeh report nahi ban sakti. "
                    "Aap status, courier, city, payment mode ya charges ke baare mein pooch sakte hain.",
    },
    "status_mismatch": {
        "en": "Your status filter is set to {selected}, but your question is about {wanted} shipments. "
              "Change the status filter to {wanted} or All Statuses and try again.",
        "hi": "आपका स्टेटस फ़िल्टर {selected} पर है, लेकिन आपका सवाल {wanted} शिपमेंट के बारे में है। "
              "स्टेटस फ़िल्टर को {wanted} या All Statuses पर करके फिर से कोशिश करें।",
        "hinglish": "Aapka status filter {selected} par hai, lekin aapka sawaal {wanted} shipments ke baare mein hai. "
                    "Status filter ko {wanted} ya All Statuses karke dobara try karein.",
    },
    "not_understood": {
        "en": "We couldn't work out which report you need. Try something like: \"How many shipments did each courier "
              "deliver this week?\"",
        "hi": "हम समझ नहीं पाए कि आपको कौन-सी रिपोर्ट चाहिए। ऐसे पूछकर देखें: \"इस हफ़्ते हर कूरियर ने कितने शिपमेंट डिलीवर किए?\"",
        "hinglish": "Hum samajh nahi paaye ki aapko kaunsi report chahiye. Aise poochh kar dekhein: \"Is hafte har courier "
                    "ne kitne shipments deliver kiye?\"",
    },
    "unrelated": {
        "en": "AI Box can answer questions about your shipments and orders. Try something like: \"Show my delivered "
              "shipments by courier.\"",
        "hi": "AI Box आपके शिपमेंट और ऑर्डर से जुड़े सवालों के जवाब देता है। ऐसे पूछकर देखें: \"कूरियर के हिसाब से मेरे डिलीवर हुए शिपमेंट दिखाओ।\"",
        "hinglish": "AI Box aapke shipments aur orders ke sawaalon ke jawaab deta hai. Aise poochh kar dekhein: "
                    "\"Courier ke hisaab se mere delivered shipments dikhao.\"",
    },
}

SUGGESTIONS = (
    "How many shipments did each courier deliver this week?",
    "Show my shipment status breakdown for the selected dates.",
    "What are my total and average freight charges?",
)


def language(prompt: str | None) -> str:
    """'hi' for Devanagari, 'hinglish' for Roman-script Hindi, otherwise 'en'."""
    text = prompt or ""
    if _DEVANAGARI.search(text):
        return "hi"
    words = re.findall(r"[a-z]+", text.casefold())
    return "hinglish" if sum(w in _HINGLISH for w in words) >= 2 else "en"


def _day(value: date | str | None) -> str:
    if isinstance(value, str):
        value = date.fromisoformat(value)
    return f"{value.day} {value:%b %Y}" if value else ""


def period(start: date | str | None, end: date | str | None, lang: str = "en") -> str:
    """"21 Oct 2026 and 27 Oct 2026" (or the Hindi joiner); one day reads as that day."""
    first, last = _day(start), _day(end)
    if not first and not last:
        return {"en": "the selected dates", "hi": "चुनी गई तारीख़ों", "hinglish": "chuni gayi dates"}[lang]
    if first == last or not last or not first:
        return first or last
    return f"{first} {'और' if lang == 'hi' else 'aur' if lang == 'hinglish' else 'and'} {last}"


def message(key: str, prompt: str | None = None, **values: Any) -> str:
    lang = language(prompt)
    return _TEXT[key][lang].format(**values)


def status_phrase(label: str | None, lang: str) -> str:
    """" with status Pickup Scheduled" when a status is selected; empty for All Statuses."""
    if not label or label.strip().lower() in {"all", "all statuses", "all status"}:
        return ""
    return {"en": f" with status {label}", "hi": f" ({label})", "hinglish": f" ({label} status)"}[lang]
