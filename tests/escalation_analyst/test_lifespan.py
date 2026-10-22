from contextlib import asynccontextmanager, nullcontext
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.escalation_analyst import application


@pytest.mark.parametrize("request_fails", [False, True])
def test_mount_preserves_host_lifespan_and_closes_runtime(monkeypatch, request_fails):
    events = []
    monkeypatch.setattr(
        application,
        "Runtime",
        lambda _: SimpleNamespace(
            start_background=lambda: events.append("module_start"),
            close=lambda: events.append("module_close"),
        ),
    )

    @asynccontextmanager
    async def lifespan(app):
        events.append("host_start")
        try:
            yield {"host_state": True}
        finally:
            events.append("host_close")

    host = FastAPI(lifespan=lifespan)
    application.install(host)
    with pytest.raises(RuntimeError) if request_fails else nullcontext():
        with TestClient(host) as client:
            assert client.app_state["host_state"] is True
            assert events == ["host_start", "module_start"]
            if request_fails:
                raise RuntimeError("test")
    assert events == ["host_start", "module_start", "module_close", "host_close"]
