"""Request/response models for the public API."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class ProjectOut(BaseModel):
    id: str
    name: str
    created_at: str
    updated_at: str


class ProjectListOut(BaseModel):
    items: list[ProjectOut]
    total: int
