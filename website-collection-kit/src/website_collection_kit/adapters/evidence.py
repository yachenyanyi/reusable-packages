"""Evidence storage adapters.

The core only receives an ``EvidenceRef``.  These adapters deliberately do not
become the package's long-term snapshot store.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from threading import Lock
from uuid import uuid4

from ..contracts import EvidenceRef, _evidence_kind_for_ref, content_sha256
from ..ports import EvidencePayload


def _make_ref(payload: EvidencePayload) -> EvidenceRef:
    digest = content_sha256(payload.content)
    kind = _evidence_kind_for_ref(payload.kind)
    return EvidenceRef(
        ref=f"evidence://sha256/{digest}/{kind}",
        kind=payload.kind,
        sha256=digest,
        media_type=payload.media_type,
        size_bytes=len(payload.content),
    )


def _safe_headers(headers: Mapping[str, str]) -> dict[str, str]:
    blocked = {"authorization", "cookie", "set-cookie", "proxy-authorization"}
    return {
        key.lower(): value
        for key, value in headers.items()
        if key.lower() not in blocked
    }


class FileEvidenceStore:
    """Content-addressed evidence store rooted at an explicit directory."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    def save(self, payload: EvidencePayload) -> EvidenceRef:
        ref = _make_ref(payload)
        with self._lock:
            digest_dir = self.root / ref.sha256[:2] / ref.sha256[2:4]
            digest_dir.mkdir(parents=True, exist_ok=True)
            content_path = digest_dir / f"{ref.sha256}.bin"
            if not content_path.exists():
                # A per-writer temporary path avoids cross-process races on
                # Windows, where replacing a file that another writer still
                # has open can fail.  The final path remains content-addressed.
                temporary = digest_dir / f".{ref.sha256}.{uuid4().hex}.tmp"
                try:
                    temporary.write_bytes(payload.content)
                    if not content_path.exists():
                        temporary.replace(content_path)
                except (FileExistsError, PermissionError):
                    # Another process may have published the same digest just
                    # before the replace.  It is safe to accept that winner
                    # only when the final object is now present.
                    if not content_path.exists():
                        raise
                finally:
                    temporary.unlink(missing_ok=True)
        return ref

    def read(self, ref: EvidenceRef | str) -> bytes:
        value = ref.ref if isinstance(ref, EvidenceRef) else ref
        match = re.fullmatch(r"evidence://sha256/([0-9a-f]{64})/[a-zA-Z0-9_-]+", value)
        if not match:
            raise ValueError("invalid evidence reference")
        digest = match.group(1)
        path = self.root / digest[:2] / digest[2:4] / f"{digest}.bin"
        return path.read_bytes()
