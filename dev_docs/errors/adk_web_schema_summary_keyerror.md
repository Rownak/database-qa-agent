# Error: `KeyError: Context variable not found: schema_summary` in `adk web`

## Cause
`INSTRUCTION` in `data_agent/agent.py` contains the placeholder `{schema_summary}`. ADK fills it from the **session state** each time the agent runs.

- `dev_test/phase_1_4.py` works because it creates the session with `state={'db_path', 'column_map', 'schema_summary'}` after `load_file`.
- `adk web` creates its own session with **empty state**. No file has been ingested, so `schema_summary` does not exist and ADK raises the `KeyError`.

The same would happen to the tools: with no `db_path` in state they return "No file has been loaded for this session."

## Possible solutions
1. **Don't use `adk web` for this agent (recommended).** The real flow is Streamlit (Phase 1.5), which calls `load_file` and creates the session with the state. Test with `dev_test/phase_1_4.py` until then.
2. **Make the placeholder optional:** use `{schema_summary?}` in `INSTRUCTION`. ADK then fills in an empty string when the key is missing, so `adk web` starts, but the agent has no schema and the tools return "No file loaded". Also add an instruction line such as "If no schema is shown, ask the user to upload a file."
3. **Seed state for `adk web`:** add a `before_agent_callback` (or a small wrapper agent module) that loads `sample_data/sales.csv` via `load_file` and sets `db_path`, `column_map`, `schema_summary` when they are missing. This adds a callback, which the design says the agent does not have, so use only for dev.
4. **Pass state when creating the session** in `adk web` (Dev UI "State" panel / API `POST /apps/{app}/users/{u}/sessions/{id}` with a `state` JSON) with the three keys. Works, but manual each time.
