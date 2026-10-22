"""Validated setup facts that must survive into the first CORSIA-agent wake."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator


class ToolAttempt(BaseModel):
    """One setup-time CLI install or sign-in attempt, safe to hand to the first chat."""

    model_config = ConfigDict(extra="forbid")

    attempt: Literal["none", "installed", "install_failed", "signin_failed"]
    cause: str | None = Field(default=None, max_length=80, pattern=r"^[a-z][a-z0-9_]*$")
    ref: str | None = Field(default=None, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")

    @model_validator(mode="after")
    def _failure_has_a_safe_cause(self) -> "ToolAttempt":
        if self.attempt in {"install_failed", "signin_failed"} and self.cause is None:
            raise ValueError("a failed attempt needs a cause code")
        if self.attempt in {"none", "installed"} and (self.cause is not None or self.ref is not None):
            raise ValueError("only a failed attempt may include cause or ref")
        return self


# The selected model belongs to the thread binding and is read when the wake renders.


class SetupHandoff(BaseModel):
    """The durable facts setup learned that the first-run wake cannot reconstruct live."""

    model_config = ConfigDict(extra="forbid")

    tools: dict[str, ToolAttempt]
    #: Which subscriptions the person said they pay for; absent when the question was not asked.
    subscriptions: dict[str, StrictBool] | None = None

    @field_validator("tools")
    @classmethod
    def _both_tool_attempts_are_present(cls, value: dict[str, ToolAttempt]) -> dict[str, ToolAttempt]:
        if set(value) != {"claude-code", "codex"}:
            raise ValueError("tools must include exactly claude-code and codex")
        return value

    @field_validator("subscriptions")
    @classmethod
    def _both_subscription_answers_are_present(cls, value: dict[str, bool] | None) -> dict[str, bool] | None:
        if value is not None and set(value) != {"claude-code", "codex"}:
            raise ValueError("subscriptions must include exactly claude-code and codex")
        return value
