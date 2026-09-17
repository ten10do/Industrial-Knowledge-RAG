# Layout-Aware Industrial Document Ingestion — LA-0 Feasibility Foundation

## Status

`LAYOUT_ANNOTATION_REQUIRED`

This phase establishes an isolated, deterministic evaluation foundation. It does not change production ingestion, retrieval, Evidence, answer generation, defaults, or the frozen correctness benchmark. Synthetic results are diagnostic only and are not authority for a real-data GO decision.

## Problem

The released ingestion path receives page text and chunks flattened lines. It preserves page ranges and coarse section metadata, but it has no page geometry, table-region, row, column, or header-to-value ownership authority. Once a table is flattened, later retrieval and Evidence stages cannot safely reconstruct which header governs a value. This can permit corpus-supported but structurally unsupported answers.

## Hypothesis

Page geometry can add a reliable structural signal if it can simultaneously achieve high header-value ownership precision, low false merges, and exact page provenance. Table-region detection alone is insufficient.

## Frozen gates

The gates were recorded before evaluation and are not tuned from the observed results.

| Metric | Gate |
|---|---:|
| Block type accuracy | >= 0.90 |
| Table region recall | >= 0.90 |
| Section attribution accuracy | >= 0.95 |
| Header-value ownership precision | >= 0.95 |
| False merge rate | <= 0.02 |
| Page provenance accuracy | = 1.00 |
| Stable-ID reproducibility | = 1.00 |
| Parser fatal failure rate | <= 0.01 |

The decisive safety metric is header-value ownership precision. A synthetic-only pass can never produce GO.

## Architecture

The research package is isolated under `backend/layout_research/` and is not imported by production code.

```text
PDF / synthetic page geometry
  -> normalized top-left page coordinates
  -> layout blocks and independently detected table regions
  -> rows, columns, cells, sections and ownership edges
  -> structured chunk candidates (research output only)
  -> deterministic evaluator
```

The coordinate contract is `x0, y0, x1, y1` in points with a top-left origin, x increasing rightward and y increasing downward. Page width, height and rotation are explicit. `normalize_bbox()` is the authority at parser boundaries.

Stable IDs hash document ID, page, normalized geometry, content and entity role. Three consecutive synthetic runs must have identical complete output digests.

## Data model

Typed dataclasses cover `DocumentLayout`, `PageLayout`, `LayoutBlock`, `TableRegion`, `TableRow`, `TableColumn`, `TableCell`, `HeaderRelationship`, and `StructuredChunkCandidate`. Every block, region, cell and candidate carries document/page lineage and source geometry.

`HEADER_APPLIES_TO` is the ownership authority. When peer headers conflict, the parser emits `AMBIGUOUS` rather than forcing an edge.

## Dataset design

The tracked public set contains 12 fully synthetic, human-authored geometry fixtures:

1. two-column specification table
2. three-model comparison table
3. merged header
4. unit column
5. range value
6. section plus table
7. borderless aligned columns
8. caption plus table
9. multi-line cell
10. ambiguous ownership
11. continued table page
12. ordinary text page

The set covers all required evaluation slices: ordinary text, simple table, borderless table, merged header, model comparison, multi-column, cross-page and mixed layout.

The local private inventory is stored outside the repository. It examined 54 PDF files, identified 16 candidate documents, and produced a 40-page DEV annotation worksheet. Candidate page distribution is 25 ordinary-text, 13 model-comparison, one simple-table, and one borderless-table page. These are automated sampling labels, not gold. No merged-header, multi-column, cross-page or mixed-layout candidate was identified by the bounded inventory heuristic; those slices remain explicit annotation gaps.

No human-verifiable private gold exists. The pack status is `ANNOTATION_PACK_READY`, not `GOLD_READY`. DEV and FINAL_GATE are separate schema values; the final gate remains unconsumed.

## Parser candidates

- `FLAT_BASELINE`: one flattened page block, with no layout inference, table region, cell, or ownership edge.
- `PYPDF_GEOMETRY`: Candidate A. Uses local text operators, coordinates and font metadata from the already pinned pypdf package; table candidates derive from page alignment rather than flattened chunk adjacency.
- `PDFPLUMBER_TABLE`: Candidate B. Uses pdfplumber's independent table finder and word geometry. Its dependency chain is research-only and is not included by production requirements.

OCR is not a default path. The private worksheet records text-layer availability so scanned pages can be separated before any later OCR experiment.

## Dependency and licensing audit

The exact machine-readable audit is in `backend/layout_research/dependency_audit.json`; pins are isolated in `backend/requirements-layout-research.txt`.

- pypdf 6.16.2: BSD-3-Clause; already production-pinned.
- pdfplumber 0.11.10: MIT; research-only Candidate B.
- pdfminer.six 20260107: MIT; research-only dependency.
- Pillow 12.3.0: MIT-CMU; research-only dependency.
- pypdfium2 5.13.0: BSD-3-Clause / Apache-2.0; research-only dependency.
- cryptography 50.0.1: Apache-2.0 OR BSD-3-Clause; research-only dependency.
- cffi 2.1.1: MIT-0; research-only dependency.
- pycparser 3.0: BSD-3-Clause; research-only dependency.
- charset-normalizer 3.5.1: MIT; research-only dependency.

PyMuPDF is not installed or used. The prior AGPL/commercial licensing concern remains a blocking reason for production adoption.

## Evaluator contract

The evaluator is deterministic and contains no LLM judge. It reports block type accuracy, table-region precision/recall/F1, section attribution, reading order, ownership precision/recall/F1, false ownership rate, false merge rate, page provenance, stable-ID reproducibility, parser fatal failure rate, per-slice results and an error taxonomy.

`FALSE_MERGE` is computed when one predicted row or column group contains matched cells belonging to more than one human-gold row or column group. Ownership precision counts only exact matched `HEADER_APPLIES_TO` pairs; `AMBIGUOUS` is never silently converted to a match.

Mutation tests cover wrong header, swapped value, wrong page, wrong table region, merged columns, missing ownership and extra ownership. Each mutation must lower the relevant metric or increment its error category.

The CLI supports reproducible preparation and evaluation:

```text
python -m backend.layout_research prepare --input <private-pdf-root> --output <outside-repo-dir>
python -m backend.layout_research evaluate --parser synthetic_geometry --gold <fixture.json> --input <fixture.json> --output <report.json>
python -m backend.layout_research compare --input <report-a.json> <report-b.json> --output <comparison.json>
```

Evaluation writes JSON and a concise Markdown summary. Private output must remain ignored or outside the repository.

## Synthetic results

| Metric | Geometry candidate | Flat baseline |
|---|---:|---:|
| Block type accuracy | 0.939394 | 0.000000 |
| Table region precision | 1.000000 | 0.000000 |
| Table region recall | 0.909091 | 0.000000 |
| Table region F1 | 0.952381 | 0.000000 |
| Section attribution accuracy | 0.939394 | 0.000000 |
| Reading order accuracy | 0.991667 | 0.000000 |
| Header-value ownership precision | 0.981132 | 0.000000 |
| Header-value ownership recall | 0.896552 | 0.000000 |
| Header-value ownership F1 | 0.936937 | 0.000000 |
| False ownership rate | 0.018868 | 0.000000 (no predictions) |
| False merge rate | 0.017857 | 0.000000 (no groups) |
| Page provenance accuracy | 1.000000 | 1.000000 |
| Stable-ID reproducibility | 1.000000 | 1.000000 |
| Parser fatal failure rate | 0.000000 | 0.000000 |

The geometry candidate fails the synthetic section-attribution gate (0.939394 versus 0.95) and meets the other numerical frozen gates on this set. This remains non-authoritative because the core gates require real human gold.

### Slice result summary

Borderless-table, cross-page, merged-header, mixed-layout, model-comparison and simple-table synthetic slices are perfect on the implemented metrics. The multi-column slice is the dominant weakness: table recall 0.666667, section attribution 0.769231, ownership precision 0.857143, ownership recall 0.500000 and false merge rate 0.083333. Ordinary text has correct block/section/order behavior and no applicable table or ownership positives.

### Dominant failure modes

- one table missed
- one column split and one column merge
- one header misbinding
- four section attribution mismatches downstream of the missed table
- one reading-order error
- one explicit ambiguous case

## Historical failure-mode comparison

- `PARAM_ROW unavailable due flattening`: synthetic geometry now produces explicit rows, columns, cells and ownership edges; real-data authority is still absent.
- cross-column ranges: the ordinary range fixture succeeds, while the ambiguous multi-column fixture remains the dominant failure, so the historical risk is not closed.
- unit-family mismatch: the unit-column fixture carries a distinct unit column, but no real human gold yet proves safe unit binding.
- scale-factor/catalog fragments: not represented by current private annotations and therefore unresolved.
- adjective-value misbinding: not separately proven; it remains a required DEV annotation target.

The new pipeline adds structural signal that the flat baseline cannot represent, but it has not yet shown that the signal is reliable on industrial PDFs.

## Private parser smoke

Both candidates completed a read-only smoke run on one sanitized six-page private document with zero fatal page failures. Candidate A emitted 854 blocks, 51 table regions and 152 ownership edges; Candidate B emitted 977 blocks, six table regions and 327 ownership edges. The large disagreement is not a score and must not be interpreted as Candidate B being better. It is evidence that parser-specific output needs human table and ownership gold before selection.

## Verification

- layout research tests: 19 passed
- evaluator mutations: wrong header, swapped value, wrong page, wrong region, merged columns, missing ownership and extra ownership all detected
- clean isolated backend suite: 991 passed, 2 skipped, 5 subtests passed
- frontend suite: 34 passed across nine files
- protected research hashes: 28/28 PASS
- dependency policy: PASS
- tracked private artifact audit: PASS, zero paths

An initial backend run inherited an ignored local `backend/light_indexes/default.json` and one existing empty-knowledge-base test reloaded that persisted index. Re-running the complete suite with an isolated temporary runtime data directory passed. No local runtime data was deleted or modified.

## Decision

`LAYOUT_ANNOTATION_REQUIRED`

Reason: no real human-verifiable ownership gold exists. Synthetic metrics, even where they meet gates, cannot establish real-data ownership precision, false merge rate or parser failure rate. The final gate remains sealed.

## Known limitations

- pypdf text-run widths are approximated where the PDF content stream does not expose glyph boxes directly.
- Candidate B is implemented but not scored without human gold.
- The bounded private inventory produces candidate slices, not verified document types or annotations.
- No OCR route is evaluated.
- Cross-page ownership continuity is represented synthetically but not established on real data.
- Structured chunk candidates are not connected to Chroma or any production runtime.

## Next engineering step

`HUMAN_LAYOUT_GOLD_ANNOTATION`

Annotate the 40-page DEV worksheet, deliberately add real merged-header, multi-column, cross-page and mixed-layout pages if available, mark uncertain ownership `AMBIGUOUS`, then evaluate both candidates. Do not consume FINAL_GATE until parser/config choices are frozen from DEV results.
