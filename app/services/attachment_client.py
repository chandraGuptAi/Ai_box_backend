"""Authenticated internal calls; attachment-only processing never needs Bigship credentials."""
import os

import httpx

from app.errors import AppError

UNAVAILABLE = 'File processing is temporarily unavailable. Please try again in a few minutes.'
PLANNING = "We couldn't work out how to build a report from this file. Try rephrasing your request and analyse again."
ATTACHMENT_MAX_BYTES = 10 * 1024 * 1024
TOO_LARGE = 'This file is larger than 10 MB. Please upload a file up to 10 MB, or split it into smaller files.'

# Ai_Box_Service returns technical messages; map them (by phrase, case-insensitive) to text an AI Box user can act on.
FRIENDLY_MESSAGES = [
    ('attachment is empty','This file is empty. Please choose a file that has data in it.'),
    ('upload size limit', TOO_LARGE),
    ('workbook exceeds cell/column', 'This spreadsheet is too large to read (more than 500,000 cells or 200 columns). '
                                     'Remove unused rows/columns or split it into smaller files.'),
    ('sheet limit', 'This workbook has too many sheets (max 100). Remove unused sheets and try again.'),
    ('slide limit', 'This presentation has too many slides (max 100). Split it into smaller files.'),
    ('page limit', 'This PDF has too many pages (max 100). Split it into smaller files.'),
    ('text exceeds extraction', 'This document has too much text to read at once. Split it into smaller files.'),
    ('archive exceeds extraction', 'This file is too complex to read. Save it again or split it into smaller files.'),
    ('image dimensions', 'This image is too large. Please upload a smaller or lower-resolution image.'),
    ('embedded image exceeds', 'This file contains an image that is too large to read.'),
    ('too many visual pages', 'This file has too many pages or images to read at once. Split it into smaller files.'),
    ('visual preparation timed out', 'Reading this file took too long. Use fewer pages or images and try again.'),
    ('visual table exceeds column', 'The table in this image has too many columns to read.'),
    ('legacy doc/xls/ppt', 'Old .doc, .xls and .ppt files are not supported. Save it as .docx, .xlsx or .pptx and upload again.'),
    ('macro-enabled', 'Files with macros or embedded programs are not supported. Save it as a normal .docx/.xlsx/.pptx file.'),
    ('supported image formats', 'This file type is not supported. Upload Excel (.xlsx), Word (.docx), PowerPoint (.pptx), PDF or an image (PNG, JPEG, WebP, TIFF, GIF, BMP).'),
    ('unsupported office archive', 'This file type is not supported. Upload Excel (.xlsx), Word (.docx), PowerPoint (.pptx), PDF or an image (PNG, JPEG, WebP, TIFF, GIF, BMP).'),
    ('invalid office archive', 'This file appears to be damaged. Open it, save it again and re-upload.'),
    ('damaged, encrypted', 'This file is damaged or password-protected. Remove the password, save it again and re-upload.'),
    ('password-protected', 'Password-protected PDFs are not supported. Remove the password and upload again.'),
    ('unreadable content', 'We could not read this image clearly. Upload a sharper image or the original spreadsheet/document.'),
    ('worksheet has no cell data', 'This spreadsheet has no data in it.'),
    ('no usable table/header', 'We could not find a table with column headers in this file.'),
    ('no cached values', 'This spreadsheet has formulas without saved results. Open it in Excel, save it, and upload again.'),
    ('mixed currencies', 'This data mixes different currencies. Keep one currency per file or column and try again.'),
    ('invalid date', 'A date column contains values that are not valid dates. Please fix them and try again.'),
    ('unreadable values', 'A number column contains values that are not numbers. Please fix them and try again.'),
    ('attachment not found', 'This file is no longer available. Please upload it again.'),
    ('planning context too large', 'Too much file data selected at once. Remove some attachments and try again.'),
    ('vision', UNAVAILABLE),
    ('skill is not installed', UNAVAILABLE),
    ('previews are not configured', 'Previews are not available for this file, but it can still be used in a report.'),
]


def friendly_message(detail, status):
    text = (detail or '').lower()
    for phrase, message in FRIENDLY_MESSAGES:
        if phrase in text:
            return message
    if status == 401:
        return 'Your session has expired. Please log in again.'
    if status == 413:
        return 'This file is too large to process. Please upload a smaller file.'
    if status == 415:
        return 'This file type is not supported. Upload Excel (.xlsx), Word (.docx), PowerPoint (.pptx), PDF or an image (PNG, JPEG, WebP, TIFF, GIF, BMP).'
    if status == 422:
        return PLANNING
    if status >= 500:
        return UNAVAILABLE
    return 'Something went wrong with this file. Please try again.'


class AttachmentClient:
    def __init__(self, transport=None):
        self.transport = transport

    async def request(self, method, path, owner, *, body=None, files=None, binary=False):
        base = os.getenv('AI_BOX_SERVICE_URL', '').strip().rstrip('/')
        token = os.getenv('AI_BOX_SERVICE_TOKEN', '').strip()
        if not base or not token:
            raise AppError(UNAVAILABLE, code='attachment_service_not_configured', status_code=503)
        if not owner:
            raise AppError('Please log in to AI Box before attaching files.', code='ai_box_auth_required', status_code=401)
        headers = {'X-User-Id': str(owner), 'X-Ai-Box-Service-Token': token}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=5), trust_env=False,
                                         transport=self.transport) as client:
                response = await client.request(method, base + '/api/attachments' + path,
                                                headers=headers, json=body, files=files)
        except httpx.TimeoutException:
            raise AppError('Reading this file took too long. Please try again, or upload a smaller file.',
                           code='attachment_timeout', status_code=504, retryable=True) from None
        except httpx.RequestError:
            raise AppError(UNAVAILABLE, code='attachment_unavailable', status_code=503, retryable=True) from None
        if response.is_error:
            try:
                detail = response.json().get('detail')
            except (ValueError, AttributeError):
                detail = None
            raw = detail if isinstance(detail, str) else None
            raise AppError(friendly_message(raw, response.status_code), code='attachment_processing_failed',
                           status_code=response.status_code, details={'reason': raw} if raw else None,
                           retryable=response.status_code >= 500)
        if response.status_code == 204:
            return None
        return response if binary else response.json()
