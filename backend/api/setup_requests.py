"""Validated request bodies for the loopback-only setup routes."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from api.setup_handoff import SetupHandoff
from infra import user_profile


class LocalDownloadRequest(BaseModel):
    """Starting the pre-account download takes no fields."""

    model_config = ConfigDict(extra="forbid")


class SetupProgressUpdate(BaseModel):
    """One change to setup progress and its validated setup-to-chat handoff."""

    model_config = ConfigDict(extra="forbid")
    step: str | None = None
    local_ai: bool | None = None
    picks: dict[str, StrictBool] | None = None
    handoff: SetupHandoff | None = None
    completed: bool = False


class SetupReportRow(BaseModel):
    """One failed setup row as the screen shows it."""

    model_config = ConfigDict(extra="forbid")
    key: Literal["claude-code", "codex", "omlx", "api"]
    name: str = Field(min_length=1, max_length=80)
    cause: str | None = Field(default=None, max_length=400)
    # Only a reference CORSIA minted: cli-, local- or signin- and 16 hex digits.
    reference: str | None = Field(default=None, pattern=r"^(?:cli|local|signin)-[0-9a-f]{16}$")


class SetupReportRequest(BaseModel):
    """The failed rows a setup report describes."""

    model_config = ConfigDict(extra="forbid")
    rows: list[SetupReportRow] = Field(default_factory=list, max_length=8)


class FirstAccountRequest(BaseModel):
    """The account screen's fields; password confirmation remains client-side."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)
    email: str | None = Field(default=None, max_length=320)

    @field_validator("name")
    @classmethod
    def _name_is_not_blank(cls, value: str) -> str:
        """Reject whitespace before it can become a profile-file name."""
        if not user_profile.one_line(value):
            raise ValueError("name is required")
        return value
