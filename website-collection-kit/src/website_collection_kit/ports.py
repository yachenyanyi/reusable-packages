"""Ports owned by the collection core.

Adapters translate their vendor objects into these small, stable types.  No
adapter implementation is imported from this module.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from .contracts import EvidenceRef, RenderingRequirement, Scope


@dataclass(frozen=True, slots=True)
class FetchRequest:
    """An internal request for one public resource."""

    url: str
    rendering: RenderingRequirement = RenderingRequirement.STATIC
    purpose: str = "page"
    scope: Scope | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url.strip():
            raise ValueError("fetch url must be a non-empty string")
        object.__setattr__(self, "url", self.url.strip())
        if not isinstance(self.rendering, RenderingRequirement):
            try:
                object.__setattr__(
                    self, "rendering", RenderingRequirement(self.rendering)
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"unsupported rendering requirement: {self.rendering}"
                ) from exc
        if not isinstance(self.purpose, str) or not self.purpose.strip():
            raise ValueError("fetch purpose must be a non-empty string")
        object.__setattr__(self, "purpose", self.purpose.strip())


@dataclass(frozen=True, slots=True)
class FetchResponse:
    """Transport-neutral response returned by an acquisition adapter."""

    requested_url: str
    final_url: str
    status: int
    headers: Mapping[str, str]
    body: bytes
    media_type: str
    fetch_method: str
    elapsed_ms: int
    redirect_to: str | None = None
    rendering_fallback_failed: bool = False


class AcquisitionPort(Protocol):
    async def fetch(self, request: FetchRequest) -> FetchResponse:
        """Fetch one resource, translating transport failures to AcquisitionError."""


@dataclass(frozen=True, slots=True)
class EvidencePayload:
    collection_id: str
    url: str
    kind: str
    content: bytes
    media_type: str
    headers: Mapping[str, str]


class EvidencePort(Protocol):
    def save(self, payload: EvidencePayload) -> EvidenceRef:
        """Persist evidence before returning its stable reference."""
