from functools import lru_cache
import re

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile

from app.api.auth import CurrentUser, get_current_user
from app.services.attachment_client import AttachmentClient

router = APIRouter(prefix='/api/attachments', tags=['Attachments'])


@lru_cache
def get_attachment_client():
    return AttachmentClient()


def identifier(value):
    if not re.fullmatch('[a-f0-9]{32}', value):
        raise HTTPException(404, 'Attachment not found')
    return value


@router.get('/capabilities')
async def capabilities(user: CurrentUser = Depends(get_current_user), client=Depends(get_attachment_client)):
    return await client.request('GET', '/capabilities', user.id)


@router.post('', status_code=201)
async def upload(user: CurrentUser = Depends(get_current_user), file: UploadFile = File(...),
                 client=Depends(get_attachment_client)):
    try:
        blob = await file.read(20 * 1024 * 1024 + 1)
        if len(blob) > 20 * 1024 * 1024:
            raise HTTPException(413, 'Attachment exceeds the 20 MiB upload limit')
        return await client.request('POST', '', user.id,
            files={'file': (file.filename or 'attachment', blob, 'application/octet-stream')})
    finally:
        await file.close()


@router.get('/{attachment_id}')
async def inspect(attachment_id: str, user: CurrentUser = Depends(get_current_user), client=Depends(get_attachment_client)):
    return await client.request('GET', '/' + identifier(attachment_id), user.id)


@router.delete('/{attachment_id}', status_code=204)
async def delete(attachment_id: str, user: CurrentUser = Depends(get_current_user), client=Depends(get_attachment_client)):
    await client.request('DELETE', '/' + identifier(attachment_id), user.id)
    return Response(status_code=204)
