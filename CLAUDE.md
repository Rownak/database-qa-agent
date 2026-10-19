# CLAUDE.md

## Git
- Only perform Git actions when explicitly instructed.
- If asked to "git add and write a commit message", stage the relevant files (`git add`) and provide a concise, descriptive commit message.
- Commit message start with Sprint number. Ex. [Sprint-2.3]
- Commit message should be in point format instead of full paragraph. 
- **Do not create a Git commit (`git commit`)**. The developer will review and commit manually.

## When stuck
Prefer asking over guessing on: record schema changes, ambiguous spec details, or anything that would require touching a locked interface.

## Documentation

- Write Google-style docstrings for every module, class, and public function/method.
- Class docstrings: purpose, responsibility, and how it fits into the overall architecture.
- Function/method docstrings: purpose, arguments, return values, and side effects (if any).
- Add concise comments only for non-obvious logic or design decisions; explain **why**, not **what**.
- Keep documentation updated with code changes.

## Testing
- After completing each phase, add pytest tests for it under `spreadsheet-qa/tests/` and run them with `uv run pytest`.
- All tests (including earlier phases) must pass before the phase is considered done.
- Also create a short developer-run script per phase in `spreadsheet-qa/dev_test/` (e.g. `phase_1_2.py`): a few lines no assertions framework. Run with `uv run python -m dev_test.phase_1_2` from `spreadsheet-qa/`.
- Report the test command and result in the execution log.

## Task Execution Summary
After completing each task phase, create a brief execution summary by creating/updating dev_docs/execution_logs/sprint_N.md:

- What was implemented or changed.
- How the task was completed (approach, key decisions, and implementation details).
- Any files modified or created.
- Any challenges, ambiguity, assumptions, or limitations encountered.
- Any CLI commands to test and run current phase.
- Keep the summary concise and focused on helping the developer understand the completed work and current project state.