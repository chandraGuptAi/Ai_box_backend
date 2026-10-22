"""Secret-protected transport API. The bridge can ingest; it cannot execute arbitrary SQL."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import Field
from .dependencies import runtime, internal
from .models import Input, Message
from ..whatsapp.media import understand

router = APIRouter(
    prefix="/internal/whatsapp", dependencies=[Depends(internal)], tags=["Internal WhatsApp transport"]
)


class Learned(Input):
    groupId: str = Field(min_length=1, max_length=250)
    name: str = Field(min_length=1, max_length=250)


class Body(Input):
    body: str = Field(max_length=1000000)


class Media(Input):
    media: dict | None
    kind: str = Field(max_length=30)


class Command(Input):
    groupId: str = Field(min_length=1, max_length=250)
    author: str = Field(min_length=1, max_length=250)


@router.get("/groups")
def groups(r=Depends(runtime)) -> dict:
    return {"groups": r.groups.list()}


@router.post("/groups/learn")
def learn(body: Learned, r=Depends(runtime)) -> dict:
    r.groups.learn(body.groupId, body.name)
    return {"ok": True}


@router.get("/groups/{group_id}")
def group(group_id: str, r=Depends(runtime)) -> dict:
    return r.groups.transport_info(group_id)


@router.post("/messages")
def ingest(body: Message, r=Depends(runtime)) -> dict:
    from ..whatsapp.ingest import ingest as capture

    return capture(r, body.model_dump())


@router.post("/messages/{message_id}/body")
def update(message_id: int, body: Body, r=Depends(runtime)) -> dict:
    r.groups.update_body(message_id, body.body)
    return {"ok": True}


@router.post("/media")
def media(body: Media, r=Depends(runtime)) -> dict:
    return {"text": understand(body.media, body.kind, r.settings)}


@router.post("/analyse")
def command(body: Command, r=Depends(runtime)) -> dict:
    from ..errors import EscalationError

    if not r.settings.recipient or body.author != r.settings.recipient:
        raise EscalationError(403, "Only the configured owner may request command analysis")
    job, created = r.start(
        "group",
        "group:" + body.groupId,
        {"hours": 24, "deliver": True, "trigger": "command"},
        lambda _: r.analysis.group(body.groupId, {"hours": 24, "deliver": True, "trigger": "command"}),
    )
    return dict(status="started" if created else "in_progress", jobId=job["id"])
