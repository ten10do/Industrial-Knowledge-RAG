from __future__ import annotations

from .models import DocumentLayout, StructuredChunkCandidate, stable_id


def structured_chunk_candidates(layout: DocumentLayout) -> tuple[StructuredChunkCandidate, ...]:
    """Build experimental candidates only; callers must not write them to production stores."""

    chunks = []
    for page in layout.pages:
        for block in page.blocks:
            chunks.append(
                StructuredChunkCandidate(
                    chunk_id=stable_id("layout-chunk", layout.document_id, page.page_number, block.block_id),
                    text=block.text,
                    document_id=layout.document_id,
                    page_number=page.page_number,
                    section_id=block.section_id,
                    block_ids=(block.block_id,),
                    block_type=block.block_type,
                    source_bboxes=(block.bbox,),
                    table_region_id=block.table_region_id,
                    row_ids=(block.row_id,) if block.row_id else (),
                    column_ids=(block.column_id,) if block.column_id else (),
                    header_context=block.header_ids,
                )
            )
    return tuple(chunks)
