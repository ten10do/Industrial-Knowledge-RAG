# LA-1.3 Terminal Closeout Report

Status: **SEALED_INCOMPLETE**
Phase: LA-1.3 (multi-annotator layout-ownership annotation on a new unseen corpus)
Record date: 2026-09-30
Branch: `research/layout-aware-ingestion`
Machine-readable companion: [LA13_CORPUS_LINEAGE.json](LA13_CORPUS_LINEAGE.json)

## 1. Decision

LA-1.3 is closed at its observed terminal state. The phase was sealed rather than pushed to completion, and the two blocking conditions are recorded as unresolved instead of being worked around.

The decision rests on a single principle that this project has applied since V3.78. A gate that cannot be satisfied honestly is stopped and documented, and the negative or incomplete result is preserved. Writing the five outstanding units from the session that was available would have made the manifest field `annotator_id` false. Adjusting an identity claim to make a number look better is exactly the class of action the project has refused elsewhere.

Disposition: the research branch is merged into `main`, so the V3 structured-graph parser, its tests and this record stay in the mainline history. The external corpus itself is not repository-managed and was not modified.

## 2. What the phase set out to do

LA-1.3 required four things:

1. a prediction-blind document split and stratified sample freeze on a corpus the parser had never seen;
2. a primary annotator pass over every sampled unit;
3. an independent blind second pass executed under a different model;
4. a corpus lineage record binding the manifests, the annotations and the annotator identities.

The purpose was to give the V3 structured graph parser an evaluation that had not influenced its own development.

## 3. What was delivered

| Deliverable | State | Value |
|---|---|---|
| Document split, 7 manuals, zero overlap | DELIVERED | `document_overlap: 0` |
| NEW_REAL_DEV sample manifest | DELIVERED | 26 entries, digest `58df3dc8fca7bbd8…` |
| NEW_FINAL_GATE sample manifest | DELIVERED | 29 entries, digest `012d5fab7b65a03c…` |
| Split manifest | DELIVERED | 7 documents, digest `6566b732f8a6d98c…` |
| Annotation units | PARTIAL | 50 of 55 |
| Decision workspace | DELIVERED | 50 decision files, 1:1 with the annotations |
| Page renders | DELIVERED | 60 renders, 55 aligned to sampled units |
| Primary annotator digest freeze | NOT DELIVERED | `ANNOTATOR_A_*_DIGEST` never produced |
| Blind second pass | NOT STARTED | blind package directory contains 0 files |
| Corpus lineage record | NOT DELIVERED | the external designated location contains 0 files |

All three manifest digests were recomputed at closeout with the frozen recipe and match the declared values in `FREEZE_SUMMARY.json` exactly. The manifests are internally consistent.

## 4. Unit accounting

| Split | Documents | Expected | Annotated | Remaining |
|---|---|---|---|---|
| NEW_REAL_DEV | 3 | 26 | 26 | 0 |
| NEW_FINAL_GATE | 4 | 29 | 24 | 5 |
| **Total** | **7** | **55** | **50** | **5** |

| Document | Split | Expected | Annotated |
|---|---|---|---|
| `v3_doc_001` | DEV | 10 | 10 |
| `v3_doc_003` | DEV | 2 | 2 |
| `v3_doc_005` | DEV | 14 | 14 |
| `v3_doc_002` | FINAL | 9 | 9 |
| `v3_doc_004` | FINAL | 8 | 8 |
| `v3_doc_006` | FINAL | 7 | 7 |
| `v3_doc_007` | FINAL | 5 | 0 |

The mapping between manifest entries and annotation files is strictly one to one by the pair (sanitized document id, page number). There is no orphan on either side and no file violates the naming convention. The decision workspace reconciles at 50 to 50 against the annotations with no orphan on either side.

Completion ratio: 0.90909.

## 5. The outstanding units

Five units of one final-gate manual, `v3_doc_007`, at pages 10, 11, 12, 22 and 30, carrying sample ids `sA-FINAL-025` through `sA-FINAL-029`. Each of the five pages was rendered and each has a corresponding decision-file slot, so the work items are fully specified and simply unwritten.

The set is unusually concentrated. The entire remaining gap sits in a single document, which means the corpus is not uniformly incomplete, the final-gate split alone is short, and only the DEV split is fully closed.

## 6. Blocking conditions

**`ANNOTATOR_AUTHORITY_MISMATCH`.** The five outstanding units carry the manifest field `annotator_id: WORKBUDDY_ANNOTATOR_A`. Annotator A was a model distinct from the session available at closeout. Authoring those units from the available session would have produced annotations whose recorded authorship is wrong. The protocol forbids the substitution in both directions, so the units could not be written.

**`BLIND_PACKAGE_EMPTY`.** The blind-package, lineage and qualified directories each contain zero files. The independent second pass had no input, so it could neither start nor be reconstructed from existing material. Building the blind package from the available annotations would have destroyed the very independence the package exists to guarantee.

Neither condition is resolvable from inside the closeout session. Both are recorded rather than bypassed.

## 7. Discrepancy reconciled

An earlier forwarded progress report claimed 51 of 55 units, that is 25 of 29 final-gate units. The disk state is 50 of 55, that is 24 of 29. The arithmetic settles it: 51 plus the 5 outstanding units gives 56 against a total of 55, while 55 minus 50 gives 5. The progress report over-counted by one. The file-derived count is authoritative and is the one recorded here.

## 8. Verification performed at closeout

| Check | Method | Result |
|---|---|---|
| Manifest digests | Recomputed with the frozen canonical recipe | 3 of 3 internal consistency, 3 of 3 match `FREEZE_SUMMARY.json` |
| Manifest to annotation mapping | One to one on (document, page) | PASS, no orphans |
| Decision workspace reconciliation | Name to name against annotations | PASS, 50 to 50 |
| Annotation artifact integrity | SHA-256 of each of the 50 files, plus a set digest | RECORDED in the lineage companion |
| Repository protected semantics | V3.82 release guard | PASS, 28 of 28, zero drift |
| Tracked private artifacts | V3.82 release guard | PASS, 0 files |
| Layout research tests | pytest on the nine layout test modules | 329 passed |
| Parser execution | Not run | No parser and no evaluator was executed during closeout |
| Corpus mutation | Not performed | The external corpus was read only |

The V3.82 research freeze and the V3.84 release candidate manifest were both consulted. Three entries of the V3.84 protected set drift against the current tree. Attribution was verified rather than assumed: the drift originates in later mainline hardening commits that postdate the V3.84 freeze, and the research branch touches none of the affected files. The closeout introduces no new drift.

## 9. What is not claimed

- No evaluation of the V3 structured graph parser on this corpus. The corpus is short of its final-gate annotations and no parser was run against it.
- No corpus-level ownership accuracy, cell-level or otherwise.
- No verified annotator independence in practice. The two annotators were confirmed to run different models, and the blind pass that would have exercised that independence never executed.
- No permission to publish the source manuals, the annotations or the page renders. They remain outside the repository.
- No completion of LA-1.3. The word sealed describes the phase status, not a successful outcome.

## 10. Artifacts

Repository-tracked by this closeout:

- this report;
- [LA13_CORPUS_LINEAGE.json](LA13_CORPUS_LINEAGE.json), the machine-readable terminal record.

Referenced but not repository-tracked, held in the external private corpus root:

- the three frozen manifests and their freeze summary;
- 50 annotation units and 50 decision files;
- 60 page renders;
- three rejected source candidates.

The lineage record deliberately omits the corpus root path and the underlying product-document filenames, in keeping with the repository absolute-path and private-artifact gates. The corpus is referenced as an external private corpus and identified by sanitized document ids only.

## 11. Residual items

1. The external designated lineage location remains empty by choice. Writing there would have mutated a corpus that is not repository-managed and that the closeout was not authorized to change.
2. `RESEARCH_DECISIONS.md` records the V3.78 through V3.81 stop decisions and is a protected documentation file under the V3.84 freeze. The LA-1.3 terminal decision is therefore recorded in this report and indexed from the research index. Folding it into `RESEARCH_DECISIONS.md` would require a new freeze and is left to a future release pack.
3. A successor phase could regenerate the blind package and rerun LA-1.3 with a model set that satisfies the independence requirement. The five unwritten units and all 50 existing units remain available as material, and nothing in this closeout precludes that.

For continuity, the preceding phase LA-1.2 delivered the V3 structured graph parser and its generalisation tests on synthetic fixtures, and that work is unaffected by this closeout.
