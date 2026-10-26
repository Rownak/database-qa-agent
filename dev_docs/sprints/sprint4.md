# Sprint 4 — Reusable Agent Pipeline and YAML-Driven Evaluation

**Goal:** implement [design.md](../design/design.md):

- one validated YAML/Pydantic configuration for the agent (`data_agent/config.py`)
- one shared entry point (`AgentSession`) used by both the Streamlit app and the evaluation runner
- a YAML-driven evaluation (`evals/`) that saves metrics together with the resolved configuration
- no change to the deployed behaviour, and no Hydra or MLflow yet (extension points only)

Every phase ends with the following (CLAUDE.md):

- pytest tests under `db-doc-qa/tests/`, with all tests (including earlier sprints) passing (`uv run pytest`)
- a short script `db-doc-qa/dev_test/phase_4_N.py`, run with `uv run python -m dev_test.phase_4_N`
- an entry in `dev_docs/execution_logs/sprint_4_N.md`, including the test command and result
- no `git commit` (the developer reviews and commits)

| Phase | Focus |
|---|---|
| 4.1 | Config models, wired into ingestion, retrieval and intent |
| 4.2 | Shared events, agent factory and runner; the app uses them |
| 4.3 | Evaluation: dataset, 4 metrics, runner, saved results |
| 4.4 | Retrieval-only mode, local verification, docs |

**Rules for all phases**

- Defaults equal today's constants, so existing tests and the deployed agent keep their behaviour.
- `data_agent/` stays flat and keeps its name (ADK deploys that folder). Modules the UI image imports must not import ADK at module level.
- Settings are passed explicitly, never through a module global.
- Evaluation code lives in `evals/` and calls only the public functions of `data_agent/`.

---

## Phase 4.1 — Config Models

**Goal:** all tunable settings live in one Pydantic model that loads from YAML, with the current values as defaults.

- [x] **Dependencies:** add `pydantic` and `pyyaml` to `pyproject.toml` (already installed by ADK; run `uv lock`). Add `pydantic` to `requirements-ui.txt` and `data_agent/requirements.txt` with the versions from `uv.lock` (`pyyaml` only to the backend one).
- [x] **Add `data_agent/config.py`** with `ConfigDict(extra="forbid", frozen=True)` on every model: `ModelConfig`, `EmbeddingConfig`, `ChunkingConfig`, `RetrievalConfig` (`mode: hybrid | bm25 | dense`), `AgentConfig`, `DatasetConfig`, `EvalConfig`, `RunConfig` (fields and defaults as in design §3.1).
- [x] **Loading:**
  - `load_config(path, overrides) -> RunConfig`: defaults < YAML < dotted `key=value` overrides, each value parsed with `yaml.safe_load`.
  - `load_agent_config() -> AgentConfig`: reads the `agent:` block of the file in `DATA_AGENT_CONFIG`, or returns the defaults when it is unset.
  - Import `yaml` inside the functions.
- [x] **Thread the settings through** with optional keyword arguments whose defaults are the default models:
  - `doc_ingest.prepare/build` take `chunking=`; `embed`/`build` take `embedding=`.
  - `retrieval.search` takes `cfg=RetrievalConfig`; `mode` skips the BM25 or the dense step.
  - `intent.classify` keeps `model=`; the caller passes `cfg.model.intent or cfg.model.agent`.
- [x] **Keep the old module constants** as aliases of the defaults (for example `CHUNK_WORDS = ChunkingConfig().chunk_words`), so no existing import or test changes.
- [x] **Tests** (`tests/test_config.py`):
  - the defaults equal the old constants (`CHUNK_WORDS`, `EMBED_DIM`, `RRF_K`, `MIN_DENSE_SIM`, `MAX_TOP_K`, `AGENT_MODEL`)
  - an unknown YAML key and an out-of-range value (`chunk_words: 5`) raise `ValidationError`
  - an override such as `agent.retrieval.min_dense_sim=0.55` has the right type and beats the YAML value
  - `retrieval.search(..., cfg=RetrievalConfig(mode="bm25"))` returns only `matched_by == "bm25"` hits (with the existing fixtures and `fake_embed`)
  - `load_agent_config()` returns the defaults when `DATA_AGENT_CONFIG` is unset
- [x] **Dev script** `phase_4_1.py`: load `configs/eval/smoke.yaml` (create it here with the design §6 content, plus the `dataset:` path) and print the resolved config as YAML.

## Phase 4.2 — Shared Events, Agent Factory and Runner

**Goal:** the app and the future evaluation call the same code to build the agent and ask a question.

- [x] **Add `data_agent/events.py`:** a `TurnResult` dataclass (`answer`, `calls`, `intent`, `latency_s`, `error`) and `parse_events(events, latency_s=0.0)`, moved from `ui_client.py`. `ui_client` imports and re-exports it; `ask()` returns the same fields as before (`answer, calls, intent`) or the `TurnResult`, whichever keeps `app.py` and the existing tests unchanged. No ADK import.
- [x] **Agent factory in `agent.py`:**
  - `build_agent(cfg: AgentConfig) -> Agent` uses `cfg.model.agent` and sets `temperature` only when it is not `None`.
  - `make_ensure_data(cfg)` returns the callback; it passes the chunking and embedding settings to `doc_ingest.build` and the intent model to `intent.classify`, skips classification when `intent_enabled` is false (state `intent = "hybrid"`, reason `"intent_disabled"`), and stores `state["agent_config"] = cfg.model_dump(mode="json")`.
  - `ensure_data = make_ensure_data(AgentConfig())` stays importable for existing tests.
  - `root_agent = build_agent(load_agent_config())`.
- [x] **Tools read the config:** `search_documents` builds `RetrievalConfig` and `EmbeddingConfig` from `tool_context.state["agent_config"]`, falling back to the defaults when the key is missing.
- [x] **Add `data_agent/runner.py`:**
  - `files_state(table, docs)` builds `upload_name`, `upload_b64` and `docs` (`None` → samples, `[]` → no documents).
  - `AgentSession(cfg, state, session_id=None)` creates an `InMemorySessionService` and `Runner` for `build_agent(cfg)`; `ask()` (sync) and `ask_async()` return a `TurnResult` and never raise (an exception becomes `error`); `close(delete_files=True)` deletes the ADK session and, optionally, the session folder.
- [x] **Use it in `app.py` local mode:** replace the Runner and event code in `_ask` and `_local_new_session` with `AgentSession` (same id and folder; a document change calls `close(delete_files=False)`, then opens a new session). API mode is untouched.
- [x] **UI image:** add `events.py` and `config.py` to `Dockerfile.ui` and to `FILES` in `scripts/stage_ui_context.py` (`tests/test_stage_ui_context.py` checks that they match).
- [x] **Tests:**
  - `tests/test_events.py`: the existing `parse_events` cases, plus a `TurnResult` field check
  - `tests/test_agent.py`: `build_agent(AgentConfig(model=ModelConfig(agent="x")))` has `model == "x"`; `state["agent_config"]` is written; a missing `agent_config` in `search_documents` uses the defaults
  - `tests/test_runner.py`: `files_state(None, [])` has `docs == []`; `AgentSession.ask` with a patched `Runner` that yields canned events returns the expected `TurnResult`, and an exception gives `error` set and no raise
  - existing `test_app*` and `test_ui_client` suites pass unchanged
- [x] **Dev script** `phase_4_2.py`: with real credentials, `AgentSession(AgentConfig(), files_state(None, None)).ask("Where is shipment 12345?")`; print the answer, intent, tool names and latency.

## Phase 4.3 — Evaluation

**Goal:** `uv run python -m evals <config.yaml> [key=value ...]` loads a dataset, runs the agent on every case, scores it with four metrics and saves the results with the resolved config under `runs/` (design §5).

- [x] **`evals/dataset.py`:** `ExpectedSection`, `Expected` (`tools`, `sections`, `answer_contains`), `EvalCase`, `EvalDataset` (design §5.1); unique case ids; a loader that resolves paths relative to `db-doc-qa/`.
- [x] **`evals/datasets/shipments_v1.yaml`:** 5–6 cases on the sample data: `db_location` (12345), `doc_express_threshold`, `hybrid_delayed_express`, `conflict_international` (12347: 5 vs 7 days), `refuse_write`, and optionally `no_policy_refrigerated` (12349).
- [x] **`evals/metrics.py`:** one dict `METRICS = {name: fn(case, result) -> float | None}`, where `None` means the case has nothing to compare:
  - `tool_sequence_match`
  - `section_recall`
  - `answer_contains`
  - `citation_precision`

  `section_recall` and `citation_precision` read the passages from the `search_documents` responses in `TurnResult.calls`.
- [x] **`evals/report.py`:** `write_run(run_dir, cfg, rows, metrics)` writes `config.yaml` (the resolved config), `results.jsonl` and `metrics.json` (design §5.4).
- [x] **`evals/run.py`:** `run_eval(cfg, run_dir, session_factory=AgentSession) -> dict`:
  1. write `config.yaml` first
  2. for each case, open a session (`files_state` from the dataset), `ask`, and `close`; a failure becomes `error` and the run goes on
  3. score each row; aggregate the mean per metric (skipping `None`), `n`, the error count and p50/p95 latency
  4. call `write_run`; return the aggregates
- [x] **`evals/__main__.py`:** parse the arguments, call `load_config`, create `runs/<timestamp>_<name>/`, call `run_eval`, and print the aggregates.
- [x] **Housekeeping:** add `runs/` to `.gitignore`.
- [x] **Tests** (no network; `session_factory` returns canned `TurnResult`s):
  - `tests/test_eval_dataset.py`: the shipped dataset loads; a duplicate id is rejected
  - `tests/test_eval_metrics.py`: each metric on one passing and one failing `TurnResult`, and `None` when `expected` has no value for it
  - `tests/test_eval_run.py`: a full run with the fake session writes the three files; the saved `config.yaml` has the override applied; `metrics.json` has the expected means; a case that raises is counted in `errors`
- [x] **Dev script** `phase_4_3.py`: `run_eval` with `dataset.limit=2` on the live agent; print `metrics.json` and the run folder.

## Phase 4.4 — Retrieval Mode and Verification

**Goal:** a cheap no-LLM mode for tuning retrieval, and proof that the app still behaves as before.

- [x] **Retrieval mode** (`eval.mode: retrieval`) in `run.py`: build `docs.db` once with the config's chunking and embedding settings, call `retrieval.search` for each case that has `expected.sections`, and compute `section_recall` and `mrr` (add `mrr` to `METRICS`).
- [x] **Add `configs/eval/bm25_only.yaml`** (`retrieval.mode: bm25`, `eval.mode: retrieval`) as the example of an ablation.
- [x] **Tests:** with `fake_embed` and the sample documents, `mrr` and `section_recall` are 1.0 for a query that matches `3.2 Express Shipments` and drop when the expected section is `9.9`; `bm25_only.yaml` loads.
- [x] **Compare a real run:** run `smoke.yaml` and `bm25_only.yaml`; note the metrics and the cost of each in the execution log.
- [x] **Regression check of the app (no config change):** run `uv run streamlit run app.py` locally, and ask the design_v2 §8 questions (12345 hybrid, 12348, 99999, 12349, 12347, injection, "delete all rows"); the answers must keep their structure.
- [x] **Docs:** update `README.md` (new folders, `DATA_AGENT_CONFIG`, how to run an evaluation) and the module docstrings that mention constants that moved.
- [x] **Dev script** `phase_4_4.py`: run the retrieval-mode evaluation with `bm25_only.yaml` and print the metrics.

---

## Out of Scope (extension points only)

- **Hydra:** `run_eval(cfg, run_dir)` already takes a validated config and a folder. A later `@hydra.main` wrapper only converts the config and sets `run_dir`.
- **MLflow:** `report.write_run` is the only place results leave the runner. A later sink logs the same params, metrics and artifacts, selected by `eval.tracker`.
- LLM-as-judge metrics, per-case files, multi-turn cases and evaluating the deployed API.
