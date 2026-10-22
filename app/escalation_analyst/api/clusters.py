"""Cluster routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from .dependencies import runtime, user
from .models import ClusterCreate, Name, Status

router = APIRouter(dependencies=[Depends(user)], tags=["Escalation clusters"])


@router.get("/clusters")
def clusters(r=Depends(runtime)) -> dict:
    return {"clusters": r.clusters.list()}


@router.post("/clusters")
def create(body: ClusterCreate, r=Depends(runtime)) -> dict:
    r.clusters.create(body.id, body.name)
    return {"ok": True}


@router.put("/clusters/{cluster_id}")
def rename(cluster_id: str, body: Name, r=Depends(runtime)) -> dict:
    return {"cluster": r.clusters.update(cluster_id, name=body.name)}


@router.patch("/clusters/{cluster_id}/status")
def status(cluster_id: str, body: Status, r=Depends(runtime)) -> dict:
    return {"cluster": r.clusters.update(cluster_id, status=body.status == "active")}


@router.get("/clusters/{cluster_id}/groups")
def members(cluster_id: str, r=Depends(runtime)) -> dict:
    r.clusters.get(cluster_id)
    mapping = r.clusters.map()
    return {
        "groups": [
            g for g in r.groups.list() if cluster_id == "all" or mapping.get(g["group_id"]) == cluster_id
        ]
    }


@router.get("/group-cluster-map")
def memberships(r=Depends(runtime)) -> dict:
    return {"map": r.clusters.map()}
