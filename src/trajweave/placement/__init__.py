"""Stage 6 deterministic placement engine.

This package is deliberately pure: it accepts Stage 5 records and returns
serializable alternatives.  Storage, CLI, and UI code own persistence and
presentation of those alternatives.
"""

from trajweave.placement.engine import ENGINE_VERSION, build_proposals, extract_features
from trajweave.placement.generate import PlacementGenerationResult, PlacementGenerator
from trajweave.placement.models import PLACEMENT_TYPES, PlacementType

__all__ = [
    "ENGINE_VERSION",
    "PLACEMENT_TYPES",
    "PlacementType",
    "PlacementGenerationResult",
    "PlacementGenerator",
    "build_proposals",
    "extract_features",
]
