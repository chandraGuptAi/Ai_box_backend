"""Authenticated internal calls; attachment-only processing never needs Bigship credentials."""
import os

import httpx

from app.errors import AppError


class AttachmentClient:
    def __init__(self, transport=None):
        self.transport = transport

    async def request(self, method, path, owner, *, body=None, files=None, binary=False):
        base = os.getenv('AI_BOX_SERVICE_URL', '').strip().rstrip('/')
        token = os.getenv('AI_BOX_SERVICE_TOKEN', '').strip()
        if not base or not token:
            raise AppError('Attachment processing is not configured.', code='attachment_service_not_configured', status_code=503)
        if not owner:
            raise AppError('Please log in before using attachments.', code='ai_box_auth_required', status_code=401)
        headers = {'X-User-Id': str(owner), 'X-Ai-Box-Service-Token': token}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=5), trust_env=False,
                                         transport=self.transport) as client:
                response = await client.request(method, base + '/api/attachments' + path,
                                                headers=headers, json=body, files=files)
        except httpx.TimeoutException:
            raise AppError('Attachment processing timed out.', code='attachment_timeout', status_code=504, retryable=True) from None
        except httpx.RequestError:
            raise AppError('Attachment processing is unavailable.', code='attachment_unavailable', status_code=503, retryable=True) from None
        if response.is_error:
            try:
                detail = response.json().get('detail')
            except (ValueError, AttributeError):
                detail = None
            message = detail if isinstance(detail, str) else 'The attachment or its analysis plan could not be processed.'
            raise AppError(message, code='attachment_processing_failed', status_code=response.status_code)
        if response.status_code == 204:
            return None
        return response if binary else response.json()
