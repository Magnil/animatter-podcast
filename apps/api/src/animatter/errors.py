"""Domain error type and its HTTP rendering.

Every error the API returns carries a ``hint`` that tells the operator what to
do next; a bare "failed" is a bug (AGENTS.md §8).
"""

from __future__ import annotations

from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class AppError(Exception):
    """An error with a stable machine code and an actionable hint."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        hint: str = "",
        status: int = HTTPStatus.BAD_REQUEST,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.status = int(status)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _handle(_request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content={"error": {"code": exc.code, "message": exc.message, "hint": exc.hint}},
        )
