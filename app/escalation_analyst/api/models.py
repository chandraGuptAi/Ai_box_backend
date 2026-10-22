"""Request validation shared by the isolated HTTP routers."""

from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Name(Input):
    name: str = Field(min_length=1, max_length=120)


class ClusterCreate(Name):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")


class Status(Input):
    status: Literal["active", "inactive"]


class Assignment(Input):
    clusterId: str = Field(min_length=1, max_length=64)
    groupName: str | None = Field(default=None, max_length=120)


class Policy(Input):
    days: int = Field(ge=1, le=3650)
    enabled: bool


class Schedule(Input):
    time_hhmm: str = Field(pattern=r"^(?:[01]?\d|2[0-3]):[0-5]\d$")
    label: str = Field(default="", max_length=120)
    window_mode: Literal["today-so-far", "previous-day"] = "previous-day"
    enabled: bool = True


class AnalysisInput(Input):
    groupName: str | None = Field(default=None, max_length=120)
    clusterId: str = Field(default="all", max_length=64)
    fromDate: str | None = None
    toDate: str | None = None
    hours: float | None = Field(default=None, gt=0, le=8760)
    deliver: bool = False
    force: bool = False
    contextReportIds: list[int] = Field(default_factory=list, max_length=5)


class Preset(Input):
    name: str = Field(min_length=1, max_length=80)
    config: dict

    @field_validator("config")
    @classmethod
    def size(cls, value: dict) -> dict:
        import json

        if len(json.dumps(value).encode()) > 20480:
            raise ValueError("Preset exceeds 20KB")
        return value


class Message(Input):
    wa_id: str = Field(min_length=1, max_length=250)
    group_id: str = Field(min_length=1, max_length=250)
    group_name: str = Field(default="", max_length=250)
    author: str | None = Field(default=None, max_length=250)
    author_name: str | None = Field(default=None, max_length=250)
    body: str = Field(default="", max_length=1000000)
    msg_type: str = Field(default="chat", max_length=30)
    timestamp: int = Field(gt=0)
    has_media: int = Field(default=0, ge=0, le=1)
    media: dict | None = None
