"""The Docker image must contain every repository folder the application reads at runtime."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME_DIRS = ("app", "docker", "skills")  # skills/: app/report_engine/registry.py reads skills/registry.json


def test_dockerfile_copies_every_runtime_folder():
    copied = set(re.findall(r"^COPY\s+(\S+)\s+\./\1\s*$", (ROOT / "Dockerfile").read_text(), re.M))
    missing = [d for d in RUNTIME_DIRS if d not in copied]
    assert not missing, f"Dockerfile does not COPY {missing}; the container would fail at runtime"


def test_dockerignore_keeps_runtime_folders():
    ignored = {line.strip().strip("/") for line in (ROOT / ".dockerignore").read_text().splitlines()
               if line.strip() and not line.startswith("#")}
    assert not ignored & set(RUNTIME_DIRS)
