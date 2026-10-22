"""Errors scoped to the mounted application, never the AI Box handlers."""

from __future__ import annotations


class EscalationError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)
