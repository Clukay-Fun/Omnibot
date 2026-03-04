"""Pydantic schema for skillspec v0.1."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _SpecBase(BaseModel):
    model_config = ConfigDict(extra="allow")


class SkillSpecMeta(_SpecBase):
    id: str = Field(min_length=1)
    version: Literal["0.1"] = "0.1"
    title: str | None = None
    description: str | None = None
    enabled: bool = True


class SkillSpecParam(_SpecBase):
    type: str
    required: bool = False
    description: str | None = None
    default: Any | None = None


class SkillSpecMatch(_SpecBase):
    channels: list[str] = Field(default_factory=list)
    senders: list[str] = Field(default_factory=list)
    intents: list[str] = Field(default_factory=list)


class SkillSpecAction(_SpecBase):
    kind: str
    target: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)


class SkillSpecResponse(_SpecBase):
    type: str | None = None
    template: str | None = None
    include_fields: list[str] = Field(default_factory=list)


class SkillSpecError(_SpecBase):
    code: str | None = None
    message: str | None = None
    retryable: bool = False


class SkillSpecOutputPolicy(_SpecBase):
    max_chars: int | None = Field(default=None, ge=1)
    max_items: int | None = Field(default=None, ge=1)
    continuation_ttl_seconds: int | None = Field(default=None, ge=1)


class SkillSpec(BaseModel):
    """Top-level skillspec document."""

    model_config = ConfigDict(extra="forbid")

    meta: SkillSpecMeta
    params: dict[str, SkillSpecParam] = Field(default_factory=dict)
    action: SkillSpecAction
    response: SkillSpecResponse
    error: SkillSpecError
    match: SkillSpecMatch | None = None
    output_policy: SkillSpecOutputPolicy | None = None
    pagination_mode: Literal["none", "offset", "cursor"] | None = None
