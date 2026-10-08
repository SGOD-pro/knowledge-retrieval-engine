"""Identity envelope and trusted authorization context contracts.

Provides flat serialization, workspace validation, and lifecycle scoping.
"""

from typing import Any
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class TrustedAuthContext(BaseModel):
    """Server only authorization context injected by authentication middleware.

    Strictly forbidden in client request payloads.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    authorized_workspace_id: str
    principal_id: str
    principal_roles: tuple[str, ...] = ("viewer",)
    access_policy_version: str

    @field_validator("authorized_workspace_id", "principal_id", "access_policy_version")
    @classmethod
    def validate_non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("Field must be a non empty string")
        return value

    @field_validator("principal_roles", mode="before")
    @classmethod
    def coerce_roles_tuple(cls, value: Any) -> tuple[str, ...]:
        if isinstance(value, (list, tuple)):
            return tuple(str(v) for v in value)
        return value


class IdentityEnvelope(BaseModel):
    """Base identity envelope with flat serialization and boundary enforcement."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_id: str
    query_id: str | None = None
    source_id: str | None = None
    source_version: int | None = None
    snapshot_id: str | None = None
    requirement_id: str | None = None

    @field_validator("workspace_id")
    @classmethod
    def validate_workspace_id(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("workspace_id must be a non empty string")
        if any(c in value for c in ("*", "?", "%", "\\")):
            raise ValueError("workspace_id cannot contain wildcard characters")
        return value

    @field_validator("source_version")
    @classmethod
    def validate_source_version(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("source_version must be a positive integer (>= 1)")
        return value

    def validate_auth(self, auth_context: TrustedAuthContext) -> None:
        """Validate envelope workspace against server injected trusted authorization context."""
        if self.workspace_id != auth_context.authorized_workspace_id:
            raise ValueError(
                f"Envelope workspace_id '{self.workspace_id}' does not match "
                f"authorized workspace '{auth_context.authorized_workspace_id}'"
            )

    @model_validator(mode="after")
    def validate_lifecycle_consistency(self) -> "IdentityEnvelope":
        # Validate that if source_id is missing, source_version cannot be specified
        if self.source_id is None and self.source_version is not None:
            raise ValueError("source_version requires source_id to be present")
        return self
