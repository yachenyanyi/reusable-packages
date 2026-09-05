"""Actionable package-level errors."""

from __future__ import annotations


class WebsiteCollectionError(Exception):
    """Base class for errors that can be handled by a caller."""


class InvalidSpecError(WebsiteCollectionError):
    pass


class ProfileIncompatibleError(WebsiteCollectionError):
    pass


class NoAcquisitionCapabilityError(WebsiteCollectionError):
    pass


class RequiredEvidenceUnavailableError(WebsiteCollectionError):
    pass


class AcquisitionError(WebsiteCollectionError):
    """A resource-level acquisition failure with a stable code."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
