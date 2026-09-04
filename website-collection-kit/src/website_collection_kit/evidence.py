"""Small in-process evidence support owned by the core.

It is intentionally limited to direct runs and tests. Durable evidence belongs
to the filesystem/object-storage adapters.
"""

from __future__ import annotations

from collections.abc import Mapping
from threading import Lock

from .contracts import EvidenceRef, _evidence_kind_for_ref, content_sha256
from .ports import EvidencePayload


def _safe_headers(headers: Mapping[str, str]) -> dict[str, str]:
    blocked = {"authorization", "cookie", "set-cookie", "proxy-authorization"}
    return {
        key.lower(): value
        for key, value in headers.items()
        if key.lower() not in blocked
    }


class MemoryEvidenceStore:
    """Deterministic store useful for tests and small direct runs."""

    def __init__(self) -> None:
        self._items: dict[str, bytes] = {}
        self._headers: dict[str, Mapping[str, str]] = {}
        self._lock = Lock()

    def save(self, payload: EvidencePayload) -> EvidenceRef:
        digest = content_sha256(payload.content)
        ref = EvidenceRef(
            ref=f"evidence://sha256/{digest}/{_evidence_kind_for_ref(payload.kind)}",
            kind=payload.kind,
            sha256=digest,
            media_type=payload.media_type,
            size_bytes=len(payload.content),
        )
        with self._lock:
            self._items.setdefault(ref.ref, payload.content)
            self._headers.setdefault(ref.ref, _safe_headers(payload.headers))
        return ref

    def read(self, ref: EvidenceRef | str) -> bytes:
        value = ref.ref if isinstance(ref, EvidenceRef) else ref
        return self._items[value]
