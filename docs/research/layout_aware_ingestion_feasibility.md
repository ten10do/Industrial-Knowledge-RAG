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

Obtain a qualified private industrial-manual corpus, regenerate the DEV worksheet, deliberately include merged-header, multi-column, cross-page and mixed-layout pages if available, then annotate uncertain ownership as `AMBIGUOUS`. Do not evaluate candidates or consume FINAL_GATE until human DEV Gold is complete and frozen.

## LA-0.5 human annotation workflow

The generic local-only workflow is now implemented. It adds multi-label high-risk page discovery, rendered page previews, blind Candidate A/B overlays, editable blocks/tables/rows/columns/relationships, manual drawing, autosave/resume, annotation validation, DEV Gold canonical digesting and a correction ledger. The browser helper binds only to loopback. Predictions are hidden on first load and remain `human_confirmed=false` when imported.

Annotation schema v2 explicitly requires `REAL_LAYOUT_DEV_GOLD`, `split=DEV`, page status, page-level confirmation and per-item confirmation. The evaluator loader rejects drafts, non-DEV files, digest drift, unreviewed pages and any prediction item that was not explicitly human-confirmed. `AMBIGUOUS` is counted separately and is not converted into an exact ownership edge.

The private inventory was re-audited before annotation. Fifty-four local PDF files were examined. The initial broad discovery pool contained documents with table-like geometry but no industrial-manual authority. A stricter corpus-eligibility gate requires both manual/specification evidence and independent equipment, electrical, mechanical or automation evidence, while excluding obvious personal-profile material. No local PDF passed that gate.

Therefore:

- qualified real documents: 0
- qualified real DEV pages: 0
- human-confirmed Gold pages: 0
- DEV Gold consumed: no
- FINAL_GATE consumed: no
- real ownership metrics: not evaluated
- real structural slices: `REAL_SLICE_NOT_AVAILABLE`

The annotation helper itself was exercised in a real browser: default-hidden predictions, overlay switching, editable-suggestion import, autosave, reload/resume and schema error display all worked. The smoke draft was restored afterward and was not treated as Gold.

Verification after implementing the workflow:

- layout research and annotation tests: 29 passed
- clean isolated backend suite: 1001 passed, 2 skipped, 5 subtests passed
- frontend suite: 34 passed across nine files
- JavaScript syntax check: PASS
- protected research hashes: 28/28 PASS
- tracked private artifact audit: PASS, zero paths

Current LA-0.5 status is `HUMAN_ANNOTATION_ACTION_REQUIRED`, with an additional corpus-availability blocker. The owner must place actual private industrial manuals in the external corpus directory, rerun sanitized inventory and workspace preparation, then perform the human review. Parser heuristics and production runtime remain unchanged.

## LA-0.6 → LA-0.7 qualified real corpus sampling

A real industrial manual corpus later became available in the external
private corpus directory. It was independently revalidated read-only
(SHA256, page count, PDF integrity, text layer, geometry extraction)
before any sampling: 11 qualified text-native manuals, 4,983 pages,
7 vendors, zero duplicates.

Full-corpus layout qualification (research-only, discovery signals only,
never Gold) then scanned every page and emitted multi-label slice
candidates plus a `LAYOUT_RISK_SCORE`. Aggregate slice availability over
4,983 pages:

| Slice | Pages |
|---|---:|
| simple_table | 3,614 |
| mixed_layout | 3,565 |
| unit_column | 3,152 |
| section_table | 3,088 |
| cross_page | 2,813 |
| multi_column | 2,514 |
| merged_header | 1,505 |
| model_comparison | 1,178 |
| ordinary_text | 1,369 |
| parameter_code_table | 631 |
| borderless_table | 310 |
| fault_code_table | 148 |

High-risk candidate pages (risk score >= 4): 3,605.

A document-level UNSEEN FINAL-GATE RESERVE of 3 complete qualified
manuals (2,956 pages) was allocated before DEV sampling so that no DEV
page and no Final-Gate page share a manual. The reserve is recorded as
sanitized IDs and page counts only, is marked `FINAL_GATE_UNCONSUMED`,
and receives no annotation, parser tuning or error analysis.

The stratified DEV sample selects 36 pages from 7 different sanitized
manuals across 5 vendors (maximum 6 pages from any single manual), with
all twelve slice labels present, ordinary text kept non-dominant (4
pages), and cross-page candidates bound to their following page as one
associated annotation unit. The frozen DEV sample manifest digest is
recorded in the private eval directory; the manifest contains sanitized
IDs only.

The private human annotation workspace was rebuilt from the frozen
sample with rendered page previews, blind Flat-reference / Suggestion-1 /
Suggestion-2 overlays, and suggestion import that keeps every imported
item `human_confirmed=false` and page status below REVIEWED until the
owner explicitly confirms. The annotation helper was exercised
end-to-end against the real workspace (launch, manifest, page API,
import semantics, edge add/delete, AMBIGUOUS, REVIEWED rejection of
incomplete pages, restore). No AI output is Gold; no freeze was
performed. Status: `OWNER_HUMAN_LAYOUT_ANNOTATION_REQUIRED`.

## LA-0.7 annotation UX compression

The human annotation helper was extended with review modes (page overview,
table structure, ownership risk, ambiguous, final page review), a
risk-prioritized owner review queue, filters, keyboard shortcuts with undo,
bulk table-structure confirmation and bulk low-risk ownership confirmation.
Bulk actions require an explicit owner click, are recorded in a private
operation log and never set page REVIEWED by themselves. A page completion
gate rejects REVIEWED while table structures are unaccepted or HIGH/AMBIGUOUS
relations are unresolved, and a rejected REVIEWED payload never overwrites
the owner draft. Predictions remain hidden on first load; imported
suggestions keep `human_confirmed=false`.

Ownership relationships are classified LOW / MEDIUM / HIGH / AMBIGUOUS from
prediction metadata for review prioritization only; classification never
mutates parser output. The stratified DEV sample, its manifest digest and
the document-level Final-Gate reserve are unchanged. At least 20% of DEV
pages are flagged `SECOND_REVIEW_REQUIRED` as a queue for the owner. A
stale untracked runtime index was quarantined outside the repository with a
recorded SHA256, after which the isolated backend suite passes with zero
failures.

End-to-end annotation-helper validation against the real workspace:
20/20 checks passed. Backend suite: 1,024 passed, 2 skipped, 0 failed.
Frontend suite: 34 passed. Protected research hashes: 28/28 PASS. Parser
semantic diff relative to the phase start commit: zero. No Gold was
created automatically and nothing was frozen or pushed. Status remains
`OWNER_HUMAN_LAYOUT_ANNOTATION_REQUIRED`.

## LA-0.7b authority correction: structure vs ownership

Owner review of LA-0.7 identified a Gold authority risk: the bulk table
confirmation also confirmed ownership relationships. The semantics are now
strictly separated. `accept_table_structure` only marks the table region,
its row/column groups and member blocks, and records
`authority.table_structure_status`; it can never touch relationships or
`authority.ownership_status`. The guard
`STRUCTURE_ACCEPTANCE_CANNOT_AUTHORIZE_OWNERSHIP` is hardcoded in the
annotation server and asserted by tests.

Ownership enters Gold only through explicit owner actions: single-edge
confirmation, low-risk bulk confirmation (unchanged, owner-clicked), or
ownership bundle confirmation. A bundle is one header applying to a set of
values (a single interpretable rule); the server rejects bundles that mix
sources or risk classifications, and AMBIGUOUS relations can never be
accepted through a bundle. Bundle actions support accept / reject /
ambiguous and are exactly undoable through a logged
`restore_ownership_state` operation. The page completion gate now requires
table structures reviewed AND ownership explicitly confirmed AND AMBIGUOUS
explicitly handled; a rejected REVIEWED payload never overwrites the draft.

All 59 annotation drafts were verified clean (no structure-acceptance
contamination existed), so no migration was required. Mutation tests A-J
plus the authority guard pass; the layout suites count 65 passing tests;
backend 1,037 passed / 0 failed. The DEV sample, manifest digest, reserve
manuals and parser semantics are unchanged. Status remains
`OWNER_HUMAN_LAYOUT_ANNOTATION_REQUIRED`.

## LA-0.7.1 annotation runtime load fix

Owner-run browser sessions hit a runtime load failure on the real
workspace. Reproduced with a real browser and traced to two independent
frontend defects, both fixed: (1) five render helpers read the annotation
collection shape without a null guard, so the boot sequence crashed while
no page was loaded yet; (2) the dashboard renderer had been renamed out
of existence while two call sites still invoked it. The boot path now
validates every unit payload against the collection contract and reports
`ANNOTATION_UNIT_INVALID` with the missing field instead of crashing on
null access; an invalid unit is never rendered as an empty-but-valid page.

Real-browser end-to-end validation against the private workspace: boot,
preview render, all five review modes, twelve units switched (single-page,
cross-page continuation, merged header, multi column, unit column, fault
code, borderless, model comparison, second-review) with zero console
errors and zero load failures, and the draft state untouched. A scripted
API sweep validated the full collection shape and preview route for all
59 annotation units. Regression tests cover the payload contract, empty
collection legality, single/cross-page units, preview route content and
byte-identical draft immutability under loading. Backend suite: 1,044
passed, 0 failed. Parser semantics, DEV sample, manifest digest and the
Final-Gate reserve are unchanged.
