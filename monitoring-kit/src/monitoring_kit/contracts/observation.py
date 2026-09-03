"""采集观察事实的稳定契约。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from .envelope import TypedEnvelope
from .primitives import (
    CORE_CONTRACT_VERSION,
    ensure_utc,
    parse_datetime,
    require_text,
    validate_contract_version,
    validate_type_key,
)


class Presence(str, Enum):
    PRESENT = "present"
    ABSENT = "absent"


@dataclass(frozen=True, slots=True)
class IngestKey:
    gateway_key: str
    upstream_record_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "gateway_key",
            require_text(self.gateway_key, "ingest_key.gateway_key", max_length=255),
        )
        object.__setattr__(
            self,
            "upstream_record_id",
            require_text(self.upstream_record_id, "ingest_key.upstream_record_id"),
        )

    def to_dict(self) -> dict[str, str]:
        return {"gateway_key": self.gateway_key, "upstream_record_id": self.upstream_record_id}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "IngestKey":
        return cls(value["gateway_key"], value["upstream_record_id"])


@dataclass(frozen=True, slots=True)
class SubjectRef:
    namespace: str
    key: str
    identity_version: str
    canonical_uri: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "namespace", validate_type_key(self.namespace, "subject.namespace"))
        object.__setattr__(self, "key", require_text(self.key, "subject.key"))
        object.__setattr__(self, "identity_version", require_text(self.identity_version, "subject.identity_version"))
        if self.canonical_uri is not None:
            object.__setattr__(self, "canonical_uri", require_text(self.canonical_uri, "canonical_uri"))

    def to_dict(self) -> dict[str, str | None]:
        return {
            "namespace": self.namespace,
            "key": self.key,
            "identity_version": self.identity_version,
            "canonical_uri": self.canonical_uri,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SubjectRef":
        return cls(value["namespace"], value["key"], value["identity_version"], value.get("canonical_uri"))


@dataclass(frozen=True, slots=True)
class Provenance:
    source_ref: str | None = None
    upstream_job_ref: str | None = None
    upstream_external_id: str | None = None
    raw_artifact_ref: str | None = None
    collector_ref: str | None = None
    attempt_ref: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "source_ref",
            "upstream_job_ref",
            "upstream_external_id",
            "raw_artifact_ref",
            "collector_ref",
            "attempt_ref",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, require_text(value, f"provenance.{field_name}"))

    def to_dict(self) -> dict[str, str | None]:
        return {
            "source_ref": self.source_ref,
            "upstream_job_ref": self.upstream_job_ref,
            "upstream_external_id": self.upstream_external_id,
            "raw_artifact_ref": self.raw_artifact_ref,
            "collector_ref": self.collector_ref,
            "attempt_ref": self.attempt_ref,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Provenance":
        return cls(
            source_ref=value.get("source_ref"),
            upstream_job_ref=value.get("upstream_job_ref"),
            upstream_external_id=value.get("upstream_external_id"),
            raw_artifact_ref=value.get("raw_artifact_ref"),
            collector_ref=value.get("collector_ref"),
            attempt_ref=value.get("attempt_ref"),
        )


@dataclass(frozen=True, slots=True)
class Observation:
    observation_id: str
    scope_key: str
    run_id: str
    ingest_key: IngestKey
    subject: SubjectRef
    observed_at: datetime
    presence: Presence
    content: TypedEnvelope | None
    provenance: Provenance
    published_at: datetime | None = None
    received_at: datetime | None = None
    contract_version: str = CORE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for field_name in ("observation_id", "scope_key", "run_id", "contract_version"):
            object.__setattr__(self, field_name, require_text(getattr(self, field_name), field_name))
        object.__setattr__(self, "contract_version", validate_contract_version(self.contract_version))
        object.__setattr__(self, "observed_at", ensure_utc(self.observed_at, "observed_at"))
        object.__setattr__(self, "presence", Presence(self.presence))
        if self.published_at is not None:
            object.__setattr__(self, "published_at", ensure_utc(self.published_at, "published_at"))
        if self.received_at is not None:
            object.__setattr__(self, "received_at", ensure_utc(self.received_at, "received_at"))
        if self.presence is Presence.PRESENT and self.content is None:
            raise ValueError("PRESENT Observation 必须有 content")
        if self.presence is Presence.ABSENT and self.content is not None:
            raise ValueError("ABSENT Observation 不能有 content")

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "observation_id": self.observation_id,
            "scope_key": self.scope_key,
            "run_id": self.run_id,
            "ingest_key": self.ingest_key.to_dict(),
            "subject": self.subject.to_dict(),
            "observed_at": self.observed_at.isoformat(),
            "presence": self.presence.value,
            "content": self.content.to_dict() if self.content else None,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "received_at": self.received_at.isoformat() if self.received_at else None,
            "provenance": self.provenance.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Observation":
        content = value.get("content")
        return cls(
            observation_id=value["observation_id"],
            scope_key=value["scope_key"],
            run_id=value["run_id"],
            ingest_key=IngestKey.from_dict(value["ingest_key"]),
            subject=SubjectRef.from_dict(value["subject"]),
            observed_at=parse_datetime(value["observed_at"], "observed_at"),
            presence=Presence(value["presence"]),
            content=TypedEnvelope.from_dict(content) if content is not None else None,
            provenance=Provenance.from_dict(value.get("provenance", {})),
            published_at=(
                parse_datetime(value["published_at"], "published_at")
                if value.get("published_at")
                else None
            ),
            received_at=(
                parse_datetime(value["received_at"], "received_at")
                if value.get("received_at")
                else None
            ),
            contract_version=value.get("contract_version", CORE_CONTRACT_VERSION),
        )
