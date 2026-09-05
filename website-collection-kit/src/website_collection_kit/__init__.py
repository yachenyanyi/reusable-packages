"""Public API for website-collection-kit.

Heavy optional dependencies are imported only when their adapters are
constructed.  Importing the core therefore remains safe in a small worker.
"""

from .adapters import (
    FileEvidenceStore,
    HttpxAcquisition,
    HybridAcquisition,
    PlaywrightAcquisition,
)
from .collection import WebsiteCollectionKit
from .contracts import (
    Attachment,
    Budget,
    CollectionIntent,
    CollectionResult,
    CollectionSpec,
    CollectionStatus,
    CoverageReport,
    DiscoverySource,
    EvidenceRef,
    Exclusion,
    FieldHints,
    InspectionSpec,
    PageCandidate,
    PageIssue,
    PageType,
    ProfileDraft,
    RenderingRequirement,
    RouteFamily,
    RoutePattern,
    Scope,
    SiteInspection,
    SiteProfile,
    StopReason,
    Usage,
    WebsitePage,
)
from .errors import (
    AcquisitionError,
    InvalidSpecError,
    NoAcquisitionCapabilityError,
    ProfileIncompatibleError,
    RequiredEvidenceUnavailableError,
    WebsiteCollectionError,
)
from .evidence import MemoryEvidenceStore
from .network import NetworkDecision, PublicNetworkPolicy
from .ports import (
    AcquisitionPort,
    EvidencePayload,
    EvidencePort,
    FetchRequest,
    FetchResponse,
)

__all__ = [
    "AcquisitionError",
    "AcquisitionPort",
    "Attachment",
    "Budget",
    "CollectionIntent",
    "CollectionResult",
    "CollectionSpec",
    "CollectionStatus",
    "CoverageReport",
    "DiscoverySource",
    "EvidencePayload",
    "EvidencePort",
    "EvidenceRef",
    "Exclusion",
    "FetchRequest",
    "FetchResponse",
    "FieldHints",
    "FileEvidenceStore",
    "HttpxAcquisition",
    "HybridAcquisition",
    "InspectionSpec",
    "InvalidSpecError",
    "MemoryEvidenceStore",
    "NetworkDecision",
    "NoAcquisitionCapabilityError",
    "PageCandidate",
    "PageIssue",
    "PageType",
    "PlaywrightAcquisition",
    "ProfileDraft",
    "PublicNetworkPolicy",
    "ProfileIncompatibleError",
    "RenderingRequirement",
    "RequiredEvidenceUnavailableError",
    "RouteFamily",
    "RoutePattern",
    "Scope",
    "SiteInspection",
    "SiteProfile",
    "StopReason",
    "Usage",
    "WebsiteCollectionError",
    "WebsiteCollectionKit",
    "WebsitePage",
]
