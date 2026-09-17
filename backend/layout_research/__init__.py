"""Isolated layout-aware ingestion feasibility research package.

Nothing in this package is imported by the production ingestion runtime.
"""

from .models import (
    BBox,
    DocumentLayout,
    HeaderRelationship,
    LayoutBlock,
    PageLayout,
    StructuredChunkCandidate,
    TableCell,
    TableColumn,
    TableRegion,
    TableRow,
)

__all__ = [
    "BBox",
    "DocumentLayout",
    "HeaderRelationship",
    "LayoutBlock",
    "PageLayout",
    "StructuredChunkCandidate",
    "TableCell",
    "TableColumn",
    "TableRegion",
    "TableRow",
]
