"""Optional technology adapters for website-collection-kit."""

from .evidence import FileEvidenceStore
from .httpx import HttpxAcquisition
from .playwright import HybridAcquisition, PlaywrightAcquisition

__all__ = [
    "FileEvidenceStore",
    "HttpxAcquisition",
    "HybridAcquisition",
    "PlaywrightAcquisition",
]
