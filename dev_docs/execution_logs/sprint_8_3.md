# Sprint 8 — Phase 8.3 Execution Log: Configurable Intent Prompt for LakeQuest

## Summary
The intent classifier now takes its prompt from configuration. LakeQuest has its own bank-domain prompt, its agent instruction consumes the classified intent, and `lakequest_bank.yaml` turns the classifier on. With no prompt file configured everything behaves as before: the default prompt and the shipments instruction are byte-identical to the committed versions (checked by SHA-256 in a test).

## What changed
- **Config** (`data_agent/config.py`): `AgentConfig.intent_prompt_file: Path | None = None`, relative to `db-doc-qa/` like `instruction_file`, saved with forward slashes.
- **Classifier** (`data_agent/intent.py`): the prompt is data, not logic.
  - `PROMPT` renamed `DEFAULT_PROMPT` (`PROMPT` kept as an alias); `classify(..., prompt=None)` formats the given text, the default otherwise.
  - New pure `check_prompt(text)` raises `ValueError` for a missing `{question}`, an unknown placeholder or a stray brace.
  - Output schema (`IntentResult`), structured-output call and `hybrid` fallback are unchanged. No file I/O in this module.
- **Wiring** (`data_agent/agent.py`):
  - `make_ensure_data(cfg)` reads and checks the file once, when the agent is built (a bad file fails at startup, not as a silent `hybrid` fallback every turn). With `intent_enabled=false` the file is not read.
  - `classify` receives `prompt=` only when a file is configured, so the default call is exactly as before.
  - `_config_path()` is shared by `instruction_file` and `intent_prompt_file`.
- **LakeQuest instruction** (`LAKE_INSTRUCTION`): new `Classified intent ... {intent?}` line and routing rule 2: `database` → `run_sql` only; `document` → `search_documents` only; `hybrid` → follow rules 3–5 (the previous order). Later rules renumbered 3–11. With the classifier off the state holds `hybrid`, which is the previous behaviour.
- **LakeQuest prompt** `configs/prompts/intent_lakequest.md`: same placeholders and labels, defined for the bank domain, hand-written examples.
- **Config** `configs/eval/lakequest_bank.yaml`: `intent_enabled: true` and `intent_prompt_file: configs/prompts/intent_lakequest.md`.
- **README:** one sentence noting that the LakeQuest results shown are the intent-off run (reproduce with `agent.intent_enabled=false`).

## Benchmark labels are not routing inputs
Case tags and `expected.*` are read only by the metrics and scoring code. The classifier is called with `(question, schema_summary, docs_summary)` plus `model` / `prompt` only, and the session state holds data files only; both are asserted in tests. The dev script found 0 of the 40 LakeQuest questions verbatim in the prompt file.

## Files
- Modified: `data_agent/config.py`, `data_agent/intent.py`, `data_agent/agent.py`, `configs/eval/lakequest_bank.yaml`, `tests/test_agent.py` (one assertion, see below), `README.md`, `dev_docs/sprints/sprint8.md` (Phase 8.3 ticked).
- Created: `configs/prompts/intent_lakequest.md`, `tests/test_intent_prompt.py` (19 tests), `dev_test/phase_8_3.py`, this log.

## Tests
- Command (from `db-doc-qa/`): `uv run pytest` → **423 passed** (404 before, plus 19 new).
- **One existing assertion changed:** `test_lakequest_profile_uses_the_lake_instruction` allowed only three placeholders in `LAKE_INSTRUCTION`; it now also allows `{intent?}`, which this phase adds on purpose. No other existing test was touched.
- New tests (no network): default prompt and shipments instruction unchanged (SHA-256); `classify` with a given and the default prompt, schema and fallback kept; `check_prompt` rejections; the LakeQuest prompt file is valid and bank-specific; the file is read once at build time (deleted before the first turn) and passed to `classify`; relative path taken from `db-doc-qa/`; no `prompt` argument without a file; missing or invalid file fails at build time; classifier off reads no file; the LakeQuest instruction resolves every placeholder in real ADK injection and shows each intent; config default, forward slashes, `lakequest_bank.yaml` and a `config.yaml` round trip; no labels in the session state or agent config.

## Compatibility checks (live, small)
| Command | Result |
|---|---|
| `evals configs/eval/smoke.yaml dataset.limit=1` (shipments, default prompt) | 0 errors |
| `evals configs/eval/lakequest_bank.yaml agent.intent_enabled=false dataset.limit=3` | 0 errors, `tool_sequence_match` 1.00 |
| `evals configs/eval/lakequest_bank.yaml agent.latency_trace=true dataset.limit=3` (new config) | 0 errors, `tool_sequence_match` 1.00, `intent` stage p50 0.98 s |

`dev_test.phase_8_3` (3 hand-written bank questions, one per intent): both prompts returned the expected label for all three; 0.9–1.2 s per classification.

## Run it
```bash
uv run pytest
uv run python -m dev_test.phase_8_3
uv run python -m evals configs/eval/lakequest_bank.yaml dataset.limit=3                            # classifier on
uv run python -m evals configs/eval/lakequest_bank.yaml agent.intent_enabled=false dataset.limit=3 # previous behaviour
```

## Limitations
- The three-question dev check and the 3-case runs only show the plumbing works. The first three LakeQuest cases are all `hybrid`, so these runs say nothing about routing by type; routing accuracy on all 40 cases is measured in Phase 8.4.
- `LAKE_INSTRUCTION` text changed (new rule and numbering), so LakeQuest answer quality with the classifier off may differ slightly from the earlier README run; Phase 8.4 compares against that run.
- `hybridqa_*.yaml` keeps `intent_enabled: false`; HybridQA would need its own prompt and instruction first.
