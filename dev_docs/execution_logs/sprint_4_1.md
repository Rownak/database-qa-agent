# Sprint 4 — Phase 4.1 Execution Log: Config Models

## Summary
All tunable settings now live in one frozen Pydantic model (`data_agent/config.py`) that loads from YAML with dotted overrides. `doc_ingest`, `retrieval` and `intent` accept it through optional keyword arguments. Defaults equal the old constants, so behaviour is unchanged. `uv run pytest`: **229 passed** (215 earlier + 14 new).

## What was done
- **Dependencies:** `pydantic>=2.14.1` and `pyyaml>=6.0.3` in `pyproject.toml`; `uv lock` run. `pydantic==2.14.1` added to `requirements-ui.txt`; `pydantic==2.14.1` and `pyyaml==6.0.3` added to `data_agent/requirements.txt` (versions from `uv.lock`).
- **`data_agent/config.py`:** `ModelConfig`, `EmbeddingConfig`, `ChunkingConfig`, `RetrievalConfig`, `AgentConfig`, `DatasetConfig`, `EvalConfig`, `RunConfig` (design §3.1), all `extra="forbid", frozen=True`.
  - `load_config(path, overrides)`: defaults < YAML < dotted `key=value` overrides, each value parsed with `yaml.safe_load`.
  - `load_agent_config()`: reads the `agent:` block of `DATA_AGENT_CONFIG`; defaults when unset.
  - `yaml` is imported inside the functions. The module imports no ADK.
- **Settings threaded through (all optional, defaults = default models):**
  - `doc_ingest.prepare(..., chunking=)`, `embed(..., embedding=)`, `build(..., chunking=, embedding=)`
  - `retrieval.search(..., cfg=)`: `mode="bm25"` skips the dense step, `mode="dense"` skips BM25; `top_n`, `rrf_k` and `min_dense_sim` come from `cfg`
  - `intent.classify` keeps `model=`; with `None` it uses `ModelConfig().agent`. This also removes the lazy import of `agent.AGENT_MODEL`.
- **Old constants kept as aliases:** `CHUNK_WORDS`, `FALLBACK_OVERLAP`, `EMBED_*`, `TOP_N`, `RRF_K`, `MIN_DENSE_SIM`, `MAX_TOP_K`, `MAX_PASSAGE_CHARS`, `AGENT_MODEL`.
- **`configs/eval/smoke.yaml`:** design §6 content plus the `dataset.path`.

## Decisions and assumptions
- **`build` passes `embedding` to `embed` only when it is set.** Existing tests replace `embed` with two-argument fakes (`texts, task_type`); always passing a third argument would break them.
- **`MetricName`** is a `Literal` of the ten metric names in design §5.3 (including the diagnostics). `ALL_METRICS` is derived from it.
- **Extra validation beyond the design:** `temperature` 0–2, `min_dense_sim` 0–1, positive integers for `top_n`, `rrf_k`, `dim`, `max_passage_chars`.
- **`DatasetConfig.path` and `EvalConfig.output_dir`** serialise as POSIX paths, so a saved `config.yaml` is the same on Windows and Linux.
- **Not in this phase:** `agent.py` (`build_agent`, `make_ensure_data`) and `search_documents` still use the constants; they move to the config in Phase 4.2. The `intent.classify` caller is unchanged for now.

## Files
- **Created:** `data_agent/config.py`, `configs/eval/smoke.yaml`, `tests/test_config.py`, `dev_test/phase_4_1.py`, this log.
- **Modified:** `data_agent/doc_ingest.py`, `data_agent/retrieval.py`, `data_agent/intent.py`, `data_agent/agent.py` and `data_agent/tools.py` (aliases only), `pyproject.toml`, `uv.lock`, `requirements-ui.txt`, `data_agent/requirements.txt`, `tests/test_agent.py` (see below).

## Challenge: a failing test unrelated to this phase
`tests/test_agent.py::test_all_placeholders_resolve_with_a_real_state` already failed before this phase (confirmed with the changes stashed). It assigned `__class__` on a `SimpleNamespace`, which this Python rejects, and the installed ADK now reads the state through `_invocation_context.session`. I changed only the test setup: it builds a real `ReadonlyContext` over a fake invocation context. No production code changed for it.

## Tests (`tests/test_config.py`, 14 cases)
Defaults equal the old constants; unknown key and `chunk_words: 5` raise `ValidationError`; models are frozen; an override is typed (`0.55` float, `false`, `null`) and beats YAML; YAML beats defaults; malformed override; the resolved config round-trips through YAML; `load_agent_config()` with the variable unset and set; a smaller `chunk_words` makes more chunks; `EmbeddingConfig` reaches the embedding call (model, dimension, batch); `mode="bm25"` returns only `matched_by == "bm25"` hits and `mode="dense"` only dense hits.

## Commands
```
cd db-doc-qa
uv run pytest                          # 229 passed
uv run python -m dev_test.phase_4_1    # prints the resolved smoke.yaml (with min_dense_sim=0.55 override) as YAML
```

## Limitations
- `configs/eval/smoke.yaml` points to `evals/datasets/shipments_v1.yaml`, which Phase 4.3 creates; loading the config does not check that the file exists.
- No `git commit` was made.
