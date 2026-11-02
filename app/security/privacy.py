"""Privacy filter for every model request: personal data is removed, the request is never blocked.

The filter runs in Ai_Box_Service (POST /api/privacy/filter). When that service is not configured or
does not answer, the same engine runs locally (app/security/privacy_engine.py). A local
deterministic re-scan always runs last. Known identities and the removal record live in
request-local context only; they are never logged.
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache

import httpx

from app.errors import AIPrivacyError
from app.security.privacy_engine import MASK, Redactor, collect_identities, load_detector

logger = logging.getLogger('ai_box.security.privacy')
_identities = ContextVar('ai_privacy_identities', default=frozenset())
_audit_id = ContextVar('ai_privacy_audit_id', default=None)
# {'removed': {fingerprint: category}, 'filters': set of filter names used}; one per outermost context.
_ledger = ContextVar('ai_privacy_ledger', default=None)
SERVICE_TOKEN_HEADER = 'X-Ai-Box-Service-Token'


@lru_cache(maxsize=1)
def _field_names():
    from app.charts.fields import get_adapter
    adapter = get_adapter('shipment')
    return frozenset(adapter.fields.keys() | adapter.measures.keys())


def _collect(value, found):
    collect_identities(value, found, field_names=_field_names())


@contextmanager
def privacy_context(*values):
    try:
        found = set(_identities.get())
        for value in values:
            _collect(value, found)
    except Exception:
        _event('blocked', 'context_failed', {})
        raise AIPrivacyError('The privacy check could not safely inspect this request.') from None
    token = _identities.set(frozenset(found))
    audit_token = _audit_id.set(_audit_id.get() or uuid.uuid4().hex)
    ledger_token = _ledger.set(_ledger.get() or {'removed': {}, 'filters': set()})
    try:
        yield
    finally:
        _identities.reset(token)
        _audit_id.reset(audit_token)
        _ledger.reset(ledger_token)


def register_source_identities(value):
    """Called on backend-fetched records before constructing model prompts."""
    try:
        found = set(_identities.get())
        _collect(value, found)
        _identities.set(frozenset(found))
    except Exception:
        _event('blocked', 'context_failed', {})
        raise AIPrivacyError('The privacy check could not safely inspect the source data.') from None


def record_source_removals(fields):
    """Fields Ai_Box_Service removed from the fetched shipments: [{field, reason, count}], no values."""
    ledger = _ledger.get()
    if ledger is not None:
        for item in fields or ():
            key = (str(item.get('field')), str(item.get('reason')))
            ledger.setdefault('source_fields', {})[key] = max(int(item.get('count') or 0),
                                                              ledger.get('source_fields', {}).get(key, 0))


def privacy_summary() -> dict:
    """What this request removed: AI-call counts per category, filters used, and fields removed at the source."""
    ledger = _ledger.get() or {'removed': {}, 'filters': set()}
    summary = {'counts': dict(Counter(ledger['removed'].values())), 'filters': sorted(ledger['filters'])}
    if ledger.get('source_fields'):
        summary['source_fields'] = [{'field': f, 'reason': r, 'count': n}
                                    for (f, r), n in sorted(ledger['source_fields'].items())]
    return summary


_NOTE_GROUPS = (
    ('person or business name', 'person or business names',
     {'person', 'known_identity', 'labelled_name', 'honorific_name', 'surname', 'field_name'}),
    ('phone number', 'phone numbers', {'field_phone'}),
    ('long number (phone, account, order or AWB number)', 'long numbers (phone, account, order or AWB numbers)',
     {'long_identifier'}),
    ('email address', 'email addresses', {'email', 'email_obfuscated', 'field_email'}),
    ('ID document number (PAN, GST, passport)', 'ID document numbers (PAN, GST, passport)',
     {'pan', 'gst', 'passport', 'field_id'}),
    ('bank or UPI detail', 'bank or UPI details', {'ifsc', 'iban', 'upi', 'field_bank'}),
    ('address', 'addresses', {'street_address', 'field_address'}),
    ('labelled personal detail', 'labelled personal details', {'labelled_detail'}),
    ('access credential', 'access credentials', {'credential', 'field_credential'}),
    ('web link', 'web links', {'url'}),
    ('encoded block', 'encoded blocks', {'encoded'}),
)


def _source_fields_text(fields) -> str:
    """'consignee name, mobile, email, address (personal or business name, contact detail, address)' style."""
    if not fields:
        return ''
    reasons: dict[str, list[str]] = {}
    for item in fields:
        label = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', str(item['field']).replace('[]', '')).replace('.', ' ').lower()
        reasons.setdefault(item['reason'], []).append(label)
    return '; '.join(f'{", ".join(dict.fromkeys(labels))} ({reason})' for reason, labels in reasons.items())


def privacy_note(summary: dict) -> str | None:
    """The reader-facing note on what was kept from the AI, or None when nothing needs saying."""
    counts = summary.get('counts') or {}
    parts = []
    for singular, plural, categories in _NOTE_GROUPS:
        total = sum(counts.get(c, 0) for c in categories)
        if total:
            parts.append(f'{total} {singular if total == 1 else plural}')
    known = set().union(*(g[2] for g in _NOTE_GROUPS))
    if other := sum(n for c, n in counts.items() if c not in known):
        parts.append(f'{other} other personal detail' + ('s' if other > 1 else ''))
    basic = 'basic' in summary.get('filters', ())
    source = _source_fields_text(summary.get('source_fields'))
    if not parts and not basic and not source:
        return None
    note = 'Privacy:'
    if source:
        note += (f' these fields were removed from the shipment data before analysis because they are personal '
                 f'data and not needed for this report: {source}.')
    if parts:
        note += (f' {"Also" if source else "To protect personal data,"} removed before AI analysis: {", ".join(parts)}. '
                 'All figures were calculated from the complete data; only the AI-written text did not see these '
                 'details.')
    if note == 'Privacy:':
        note += ' no personal data was found.'
    if basic:
        note += (' The privacy service was unavailable, so a basic filter (number patterns and labelled names) '
                 'was used for this report.')
    elif 'local' in summary.get('filters', ()):
        note += ' The privacy service was unavailable, so the same filter ran locally.'
    return note


def _event(action, reason, counts, source=None):
    # Never log content, exception messages, field paths, IDs, hashes or matched values.
    logger.warning('%s', json.dumps({'event': 'ai_privacy', 'action': action,
                                    'audit_id': _audit_id.get() or uuid.uuid4().hex, 'reason': reason,
                                    'filter': source, 'categories': dict(+Counter(counts))}, sort_keys=True))


@lru_cache(maxsize=1)
def _local_detector():
    try:
        return load_detector()
    except Exception:
        logger.error('Local name detector unavailable; the fallback filter masks patterns and labelled names only.')
        return None


def _service_filter(messages, identities):
    """(messages, {fingerprint: category}) from Ai_Box_Service, or None when it is not configured or fails."""
    base_url = os.getenv('AI_BOX_SERVICE_URL', '').strip().rstrip('/')
    if not base_url or os.getenv('PRIVACY_FILTER_SERVICE', 'true').strip().lower() in {'0', 'false', 'no', 'off'}:
        return None
    headers = {}
    if token := os.getenv('AI_BOX_SERVICE_TOKEN', '').strip():
        headers[SERVICE_TOKEN_HEADER] = token
    try:
        with httpx.Client(timeout=float(os.getenv('PRIVACY_FILTER_TIMEOUT_SECONDS', '10') or 10),
                          trust_env=False) as client:
            res = client.post(f'{base_url}/api/privacy/filter', headers=headers, json={
                'messages': messages, 'identities': sorted(identities), 'field_names': sorted(_field_names())})
        res.raise_for_status()
        body = res.json()
        protected, removed = body['messages'], body['removed']
        if (not isinstance(protected, list) or len(protected) != len(messages) or
                any(not isinstance(m, dict) or m.get('role') != o['role'] or not isinstance(m.get('content'), str)
                    for m, o in zip(protected, messages))):
            raise ValueError
        return ([{'role': m['role'], 'content': m['content']} for m in protected],
                {str(item['ref']): str(item['category']) for item in removed})
    except Exception:
        logger.warning('Privacy service unavailable; using the local privacy filter.')
        return None


class PrivacyGuard:
    def protect(self, messages):
        if (not isinstance(messages, list) or not messages or any(
                not isinstance(m, dict) or set(m) != {'role', 'content'} or m['role'] not in {'system', 'user', 'assistant'}
                or not isinstance(m['content'], str) for m in messages)):
            # A programming error, not seller content: nothing is sent.
            _event('blocked', 'invalid_messages', {})
            raise AIPrivacyError('The AI request was malformed and was not sent.')
        found = set(_identities.get())
        _collect(messages, found)
        result = _service_filter(messages, found)
        if result is not None:
            protected, removed, source = *result, 'service'
        else:
            detector = _local_detector()
            local = Redactor(found, detector, _field_names())
            protected, removed, source = local.messages(messages), local.removed, 'local' if detector else 'basic'
        # Independent deterministic re-scan of whatever the filter returned.
        final = Redactor(found, None, _field_names())
        protected = final.messages(protected)
        removed = {**removed, **final.removed}
        ledger = _ledger.get()
        if ledger is not None:
            ledger['filters'].add(source)
            for ref, category in removed.items():
                ledger['removed'].setdefault(ref, category)
        counts = Counter(removed.values())
        _event('sanitized' if counts else 'allowed', 'checked', counts, source)
        return protected


guard = PrivacyGuard()
