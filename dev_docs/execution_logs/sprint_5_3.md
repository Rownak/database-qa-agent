# Sprint 5 — Phase 5.3 Execution Log: `bank_sample` Cases from the Snapshot

## Summary
`evals.lakequest prepare` now builds the whole `bank_sample` bundle from the public lake zip alone: `--questions` is optional, and without it the 40 cases and `reference/references.jsonl` are generated. The real bundle was built in `benchmark/lakequest_prepared/bank_sample/` (git-ignored). `uv run pytest`: **299 passed** (290 earlier + 9 new).

## What was done
- **CLI:** `--questions` is optional; `--seed` was added. Without `--questions`, `--references` is an error, and the domain must be `bank`. The Codabench path (`--questions` / `--references`) is unchanged.
- **Case generation** (`build_sample_cases` and helpers in `evals/lakequest.py`). It runs while the extracted lake database is still open, because it is the only reader of `persona_workflows`. The agent's `bank.db` is built from the whitelist as before.
  - Only validation clients are used (`split_entities.parquet`, 58 clients). The random choices use a fixed seed (`20260101`).
  - **20 `hybrid`:** the first 20 `persona_workflows` rows of validation clients, sorted by `qa_id`. `tools: [run_sql, search_documents]`; `reference_answers: [answer_direct, answer_reasoning]`; `object_ids: sources + [policy_id]`. `sections` come from "Section N" / "Sections N and M" in `answer_reasoning`, and only numbers that exist as headings in the policy are kept.
  - **10 `table`:** accounts / transactions / cheques counts for the hybrid clients ("How many … does {first} {last} (client_id: {id}) have?"). The answer is the SQL count on `bank.db` (`answer_contains` and `reference_answers`). A zero count is skipped. `object_ids` are the tables the count needs.
  - **10 `document`:** 5 "which section covers {heading}?" (`answer_contains: [N]`, `sections`) and 5 "What is the purpose of policy {id}?" (`reference_answers: [Purpose paragraph]`, section `1`). Each policy is used at most once per question type; the policies are those of the hybrid cases.
  - `tags: [bank, <type>]`; ids are the `qa_id`, `tbl-<n>`, `doc-<n>`.
- **`reference/references.jsonl`:** one row per case with `qa_id`, `domain`, `question`, `answer_short`, `answer_long`, `gold_object_ids`, `gold_provenance_ids: []`. `answer_short` / `answer_long` are the first two reference answers, or the `answer_contains` value.
- **`prepare.json`:** now has `sample` (seed, counts) and `counts.cases_per_type`. `sources.questions` is `null` in sample mode.
- **Config:** `configs/eval/lakequest_bank_dev.yaml` was renamed to `lakequest_bank.yaml` (profile `lakequest`, intent off, the seven metrics, `predictions: true`, dataset `bank_sample`). This is a Phase 5.4 task that I did now because `--config` needs it.
- **Removed** `dev_test/phase_5_2.py`: its real-data run moved to this phase, and it pointed to the removed Open Dev config.
- **Tests** (`tests/test_lakequest_prepare.py`): the fixture lake now has 4 corpus tables, a `persona_workflows` table with rows, `split_entities` and numbered policies, and the existing assertions were updated to it. New tests: counts per type and `EvalDataset` validity; hybrid fields (sections, references, object ids; the test-split client is excluded; "Section 9" is dropped as it does not exist); table answers equal the SQL counts; document section numbers and purpose paragraphs match the policy headings; `persona_workflows` is absent from `bank.db` and the schema summary; `references.jsonl` columns; the same seed gives the same `dataset.yaml`; sample-mode errors; `referenced_sections`.
- **Dev script** `dev_test/phase_5_3.py`.

## Real bundle (`bank_sample`)
| Item | Result |
|---|---|
| Cases | 20 hybrid, 10 table, 10 document (40); `references.jsonl` has 40 rows |
| `bank.db` | the 8 corpus tables; no `persona_workflows` |
| `docs.db` | 2,481 chunks, 212 policies |
| Expected sections | all found in `docs.db` (checked each `source` + `section` against the chunk section numbers) |
| Table cases | accounts 1–3, transactions 48–359 |

## Limits and assumptions
- **Only 10 of the 20 hybrid cases have `sections`:** the other `answer_reasoning` texts do not name a section number. `section_recall` is therefore `None` for them, and `tool_sequence_match` still applies to all 20.
- **`answer_contains` for section questions is a bare number** (for example `6`), so it also matches any other "6" in the answer. It is a weak check; the stronger one is `section_recall`.
- **The table question uses all the hybrid clients**, so those clients appear in several cases.
- **Section questions may be ambiguous** when a heading title repeats; the builder does not check for that.
- The bundle is not scored yet: the runner support (`state_file`, `predictions.jsonl`) is Phase 5.4, so `dataset.yaml` cannot be run through `run_eval` for now.
- Rebuilding calls the embedding API (about 20 batches, 49 s).

## Files
- Modified: `evals/lakequest.py`, `tests/test_lakequest_prepare.py`, `dev_docs/sprints/sprint5.md` (ticked 5.3 and the config task of 5.4)
- Renamed: `configs/eval/lakequest_bank_dev.yaml` → `configs/eval/lakequest_bank.yaml`
- Created: `dev_test/phase_5_3.py`, `dev_docs/execution_logs/sprint_5_3.md`
- Deleted: `dev_test/phase_5_2.py`

## Commands
```
cd db-doc-qa
uv run pytest                                  # 299 passed
uv run python -m evals.lakequest prepare --lake-zip ../benchmark/LakeQuest/public_data/public_lake_data.zip \
  --config configs/eval/lakequest_bank.yaml --out ../benchmark/lakequest_prepared/bank_sample
uv run python -m dev_test.phase_5_3            # builds the real bundle; needs the API key
```
