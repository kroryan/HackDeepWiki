"""Errors that can cross the HTTP/domain boundary safely."""

from __future__ import annotations


class DomainError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
