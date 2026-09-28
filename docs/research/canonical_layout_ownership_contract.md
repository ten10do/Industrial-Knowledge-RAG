# Canonical Layout Ownership Contract (Phase LA-1.1)

Status: `OWNERSHIP_CONTRACT_RECONCILED` (research phase; no production integration)

Branch: `research/layout-aware-ingestion`. This document records only aggregate
engineering conclusions. All gold annotations, manual pages and mapping outputs
remain private / repository external.

## 1. Problem

The legacy pipeline stored a single flat relation, `HEADER_APPLIES_TO`, and
scored it with one F1. Annotator, parser and evaluator did not share the same
ownership ontology, so a parser could fail a gate for reasons that were
ontological rather than structural. LA-1.1 defines the ontology first.

## 2. Spent gate classification

The consumed LA-1.0 Final Gate corpus (27 pages, gold digest f4c93965...) is
permanently reclassified as `SPENT_GATE_DIAGNOSTIC_CORPUS`. It may be used for
postmortem, ontology analysis and contract mapping only. It must never again be
reported as an independent test, final gate, holdout or generalization
evidence. Historical Final Gate metrics (Ownership P/R/F1 = 0.6215 / 0.1622 /
0.2573, False Ownership 0.3785, Table P/R 0.9464 / 0.8983) remain frozen and
unchanged; the new semantics do not retroactively alter them.

## 3. Relation ontology

Direct (stored, locally evidenced) relations:

`HEADER_PARENT_OF_HEADER`, `COLUMN_HEADER_OF`, `ROW_HEADER_OF`, `UNIT_OF`,
`MODEL_OF`, `VALUE_IN_COLUMN`, `VALUE_IN_ROW`, `TABLE_CONTAINS`,
`SECTION_CONTAINS`, `TABLE_CONTINUES_ON`, `CAPTION_DESCRIBES`

Derived (never stored, computed on demand): `HEADER_APPLIES_TO`.

`HEADER_APPLIES_TO` is retained only as a DERIVED context edge so historical
artifacts stay interpretable. It is never again a primary stored parser output.

Each relation carries a full contract (source/target types, geometric and
structural evidence, cardinality, ambiguity behaviour, transitivity, storage
class, evaluation rule) in `backend/layout_research/ownership_contract.py`
(`CONTRACTS`).

## 4. Direct Edge Principle

Store direct relationships; derive context. A value's ancestor chain is a
derived context closure (`derive_context`), deterministic, cycle-safe, with
stable ordering, and explicit `AMBIGUOUS` semantics: competing structural
parents at the same slot resolve to AMBIGUOUS when geometry cannot uniquely
contain the value. Nearest-text fallback is forbidden by contract.

## 5. Implementation

- `backend/layout_research/ownership_contract.py` - relation ontology,
  contract registry, 11-class geometric edge taxonomy, legacy edge mapping
  (DIRECT / DERIVED / AMBIGUOUS / OTHER), `ContextGraph.derive_context`,
  `derive_context_from_direct_edges` (cycle-safe explicit-graph variant).
- `backend/layout_research/canonical_evaluator.py` - parallel experimental
  evaluator reporting Direct Edge P/R, per-relation P/R and per-axis context
  P/R (MODEL / UNIT / ROW / COLUMN / MULTI_LEVEL / SECTION). Ambiguity itself
  is scoreable (`AMBIGUOUS` markers). Empty-vs-empty agreement scores 1.0.
- `backend/layout_research/annotation_schema_v3.json` - ANNOTATION_SCHEMA_V3:
  direct_edges, derived_context, ambiguous_relations, header_tree, row_axis,
  column_axis, spanning_headers, table_hierarchy. Frozen V2 gold is not
  migrated; V3 applies to future corpora only.
- `backend/layout_research/canonical_fixtures.py` - 22 hand-authored synthetic
  fixtures (digest-stable) covering: single-level columns, multi-level header,
  merged header, model comparison, unit row, unit column, row header,
  parameter code, fault code, nested header, spanning header, cross-page
  continuation, duplicate visual header, ambiguous model header, diagram
  adjacent to table, borderless table, range value, section heading, caption,
  sparse column, model row headers. Each fixture hand-specifies its direct
  edges, derived context and ambiguous axes.
- `backend/test_layout_ownership_contract.py` - 82 tests: contract registry
  integrity, fixture truth reproduction (mapping + derivation + evaluator
  self-agreement), and the full mutation suite (wrong direct parent, extra
  ancestor edge, missing model context, wrong unit, sibling-column leakage,
  merged-header over-propagation, row/column axis swap, duplicate parent,
  cycle, cross-page leakage, section used as cell header).

Contract decisions fixed during fixture authoring (declared in code, applied
uniformly): a row-label column header is the COLUMN_HEADER_OF parent of every
row-header cell below it; an adjacent unit/model header carries the more
specific UNIT_OF/MODEL_OF relation instead of COLUMN_HEADER_OF; unit/model
semantic roles apply only to pure values (cells that themselves emit edges
keep structural classification); positive-overlap geometry (edge contact is
not overlap).

## 6. Aggregate diagnostic (spent gate + DEV, private corpus)

Contract mapping of flat legacy edges (aggregate counts only):

| corpus | pages | edges | /page | DIRECT | DERIVED | AMBIGUOUS | OTHER |
| --- | --- | --- | --- | --- | --- | --- | --- |
| DEV model gold | 59 | 2154 | 36.5 | 1817 (84.4%) | 215 (10.0%) | 92 (4.3%) | 30 (1.4%) |
| Spent Final gold | 27 | 2460 | 91.1 | 2055 (83.5%) | 35 (1.4%) | 166 (6.7%) | 204 (8.3%) |
| V2 output (same 27 pages) | 27 | 1323 | 49.0 | 1178 (89.0%) | 105 (7.9%) | 30 (2.3%) | 10 (0.8%) |

Findings:

1. All three edge sets are dominated by DIRECT edges under canonical
   semantics. The hypothesis that the Final annotator emitted a full
   ancestor closure is not supported: only 1.4% of Final gold edges are
   derived-transitive.
2. The 36.5 vs 91.1 edges/page density gap is primarily a corpus composition
   difference (DEV and Final gate pages come from different manuals with
   different table styles; Final pages carry 1450 values over 1821 cells,
   1.70 edges/value, vs DEV 1559 values over 4638 cells, 1.48 edges/value).
3. The legacy "V2 output edges = 642" figure counts only V2 edges whose both
   endpoints map to gold cells (predicted_ownership after IoU mapping); the
   raw V2 edge count on the same pages is 1323 over 2683 parsed cells. A
   large share of the Final Gate ownership loss occurs at the cell
   segmentation level, before edge matching.
4. DEV gold (authority MODEL_ADJUDICATED/CODEX) shows a parser-vocabulary
   edge shape (row + adjacent-parent dominated, 10.4% derived flag) and its
   historical V2 F1 of 0.9986 reflects shared vocabulary, not independence.
   DEV gold remains non-independent and is never a holdout.

## 7. V2 architecture assessment

V2's direct-edge core is directionally consistent with the canonical contract
(89% direct, row/leaf dominated). The failures are (a) the flat
`HEADER_APPLIES_TO` vocabulary, which conflates direct and derived edges and
cannot express unit/model/row/column roles, and (b) coverage: V2 emits about
half of the direct edges the independent gold expects (row headers, merged
headers, unit rows), and its acceptance contract discards ambiguous slots
instead of marking them. Verdict: the semantics survive; the parser output
layer does not. A structured graph parser (PDFPLUMBER_STRUCTURED_GRAPH_V3:
table region -> cell geometry -> row/column axis -> header tree -> direct
structural edges -> context derivation -> chunk candidate) is required.
Table detection stays frozen (Table P/R 0.9464 / 0.8983 on the spent gate);
ownership semantics remain isolated from table detection.

Secondary findings (not blocking the contract line): Block type accuracy 0.678
and section attribution 0.328 on the spent gate mix gold ontology conventions
with real parser behaviour; they are recorded as SECONDARY_FINDING and do not
alter the ownership contract.

## 8. Future gate policy

- Next unseen Final Gate uses a NEW document-level holdout from fresh
  industrial manuals; the 27 spent pages and the remaining pages of
  doc_001 / doc_003 / doc_007 (SEEN DIAGNOSTIC DOCUMENTS) are excluded.
- Future gold authority requires an independent annotator with
  prediction-blind evaluation, preferably human reviewed; model annotation
  requires Annotator A + independent Annotator B with separate disagreement
  adjudication. Same-model self-evaluation is prohibited.
- No tuning against the consumed gate. Parser V3 is designed against the
  contract and fixtures, not against spent-gate metrics.

## 9. Production freeze

Release v0.1.0-rc1 unchanged; RAG integration NO; production ingestion,
retrieval, evidence and answer generation unchanged; the 69-query benchmark
not run. Production protected semantic state intact: 28 protected hashes
freeze at the release, correctness digest
`dcd548906ef2b1f233282283b8684f8c82066dc5eea9f0179ff45a4034a5aa1d`.

Protected manifest status (read-only investigation): the historical "28
protected hashes" authority IS located: `V384_RC_FREEZE_MANIFEST.json`
(repo root), key `protected_semantic_file_sha256` (28 entries), referenced by
`RELEASE_CHECKLIST.md` and the V3.84 packaging report. No new manifest is
required; none was fabricated.

## 10. Next phase

`NEXT_PHASE = STRUCTURED_GRAPH_V3_IMPLEMENTATION` (research branch; still no
production integration).
