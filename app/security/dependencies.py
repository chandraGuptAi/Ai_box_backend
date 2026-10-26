"""Capture local identity context without changing seller data or HTTP responses."""
import json
from fastapi import Request
from app.security.privacy import privacy_context


async def privacy_request_context(request: Request):
    payload = None
    if request.method in {'POST', 'PUT', 'PATCH'} and 'application/json' in request.headers.get('content-type', ''):
        try:
            payload = await request.json()
        except (ValueError, UnicodeError):
            pass  # FastAPI rejects malformed JSON; it never reaches a model workflow.
    credentials = []
    for header in ('authorization', 'x-shipment-authorization'):
        value = request.headers.get(header)
        if value:
            credentials.extend([value, value.removeprefix('Bearer ').strip()])
    identities = {'seller_name': request.headers.get('x-user-name'),
                  'seller_id': request.headers.get('x-user-id'), 'access_token': credentials}
    with privacy_context(payload, identities):
        yield
