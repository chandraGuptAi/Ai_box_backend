"""Owner-scoped report-builder presets."""

from __future__ import annotations
from fastapi import APIRouter, Depends, Response
from .dependencies import runtime, user
from .models import Preset

router = APIRouter(dependencies=[Depends(user)], tags=["Escalation presets"])


@router.get("/report-presets")
def presets(owner=Depends(user), r=Depends(runtime)) -> dict:
    return {"presets": r.preferences.presets(owner.id)}


@router.post("/report-presets", status_code=201)
def add_preset(body: Preset, owner=Depends(user), r=Depends(runtime)) -> dict:
    return {"id": r.preferences.save_preset(owner.id, body.name, body.config)}


@router.put("/report-presets/{preset_id}")
def update_preset(preset_id: int, body: Preset, owner=Depends(user), r=Depends(runtime)) -> dict:
    return {"id": r.preferences.save_preset(owner.id, body.name, body.config, preset_id)}


@router.delete("/report-presets/{preset_id}", status_code=204)
def delete_preset(preset_id: int, owner=Depends(user), r=Depends(runtime)) -> Response:
    r.preferences.delete_preset(owner.id, preset_id)
    return Response(status_code=204)
