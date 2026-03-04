"""Pydantic schema for skillspec v0.1 runtime assets."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _SpecBase(BaseModel):
    model_config = ConfigDict(extra="allow")


class SkillSpecMetaMatch(_SpecBase):
    regex: str | None = None
    keywords: list[str] = Field(default_factory=list)


class SkillSpecMeta(_SpecBase):
    id: str = Field(min_length=1)
    version: Literal["0.1"] = "0.1"
    title: str | None = None
    description: str | None = None
    enabled: bool = True
    match: SkillSpecMetaMatch | None = None


class SkillSpecMatch(_SpecBase):
    regex: str | None = None
    keywords: list[str] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=list)
    senders: list[str] = Field(default_factory=list)
    intents: list[str] = Field(default_factory=list)


class SkillSpecOutputPolicy(_SpecBase):
    max_chars: int | None = Field(default=None, ge=1)
    max_items: int | None = Field(default=None, ge=1)
    continuation_ttl_seconds: int | None = Field(default=None, ge=1)


class SkillSpec(BaseModel):
    """Top-level skillspec document with flexible sections."""

    model_config = ConfigDict(extra="forbid")

    meta: SkillSpecMeta
    params: dict[str, Any] = Field(default_factory=dict)
    action: dict[str, Any]
    response: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] = Field(default_factory=dict)
    match: SkillSpecMatch | None = None
    output_policy: SkillSpecOutputPolicy | None = None
    pagination_mode: Literal["none", "offset", "cursor"] | None = None
