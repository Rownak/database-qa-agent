# Database Q&A Agent — Project Description

A small demo project built with **Google Agent Development Kit (ADK)**. A user uploads a CSV or Excel file and asks questions about it in plain language. The app checks the file, loads it into a temporary SQLite database, and an ADK agent answers by calling Python tools and running read-only SQL.

---

## 1. Overview and Objectives

**Goal:** Get a first working version of an agent that answers questions from data, using as few moving parts as possible.

Objectives:

- Define a single ADK `LlmAgent` with clear instructions and a few function tools.
- Write Python tools that the agent calls, and see how tool results ground its answers.
- Let an LLM write SQL and run it safely (read-only, scoped to one database).
- Keep data separate for each session and clean it up when the session ends.
- Connect an ADK agent to a simple Streamlit upload-and-chat UI.

## 2. Core User Workflow

1. The user opens the Streamlit app, which starts a new session.
2. The user uploads one `.csv` or `.xlsx` file.
3. The app validates the file. If it fails, the app shows a clear error and the user can try another file.
4. If the file is valid, the app loads it into one table in a SQLite database for that session and shows a short summary: table name, row and column counts, and a preview.
5. The user asks questions in the chat. For each one, the agent:
   - uses an inspection tool, or writes and runs a read-only SQL query, and
   - answers based on the result. If the data can't answer the question, the agent says what is missing.
6. The user clicks **End session**, or the session expires. The app deletes the uploaded file and the database.

## 3. MVP Features and Boundaries

**In scope**

- One file per session, loaded as one table (for example `data`). Uploading a new file replaces the current one: the old file and database are deleted first.
- Supported formats: `.csv` (UTF-8, comma-delimited) and `.xlsx`.
- **Excel worksheets:** only the **first worksheet** is loaded. If the workbook has more sheets, the app tells the user which sheets were ignored. Choosing a sheet, or loading several, is left for later.
- Column names are cleaned into safe SQL identifiers (trimmed, spaces become `_`). The agent sees both the original and the cleaned names.
- Column types are inferred when the file is loaded (integer, real, text, date stored as text).
- Inspection tools: row and column counts, column names and types, sample rows, and basic descriptive statistics.
- A SQL tool that runs one read-only query written by the agent and returns at most a fixed number of rows (for example 100).
- Answers that are grounded in tool output and say which tool or query was used.

**Out of scope for v1**

- Multiple files, joins across files, or more than one sheet per workbook.
- Writing to the data, uploading back to the source, or exporting results.
- Charts, authentication, persistent storage, or deployment.
- Multiple agents, planning loops, or separate backend services.

## 4. Minimal Components

| Component | Responsibility |
|---|---|
| **Streamlit UI** (`app.py`) | File uploader, data summary and preview, chat box, **End session** button. Holds the session ID and the ADK runner in `st.session_state`. |
| **Ingestion module** | Reads CSV/XLSX with pandas, validates the file, cleans column names, and writes one table to `<temp>/<session_id>/data.db`. |
| **Data tools** (Python functions given to the agent) | `get_table_info()` for counts, columns and types; `get_sample_rows(n)`; `get_column_stats(column)`; `run_sql(query)` for read-only queries with a row limit. |
| **ADK agent** | One `LlmAgent` (Gemini model) whose instructions tell it to inspect the schema first, prefer a dedicated tool when one fits, use SQL otherwise, never make up values, and explain when data is missing. |
| **Session manager** | Creates the temporary folder for each session, tracks when the session was last active, and deletes files when the session ends. Uses ADK's `InMemorySessionService` for conversation state. |

Tools find the current session's database path from the session state, so the agent can only reach its own session's data.

## 5. Validation, Error Handling, and Cleanup

**Validation before loading**

- The file extension must be `.csv` or `.xlsx`. Anything else is rejected with an "unsupported file type" message.
- The file size must be under a set limit (for example 10 MB).
- The file must open without errors (bad encoding or a corrupt workbook gives a "could not read file" message).
- After parsing, the table must have at least one data row and at least one column.
- The file must have a header row. Headers must be non-blank, text (not plain numbers), and unique (no duplicate column names). If all headers look auto-generated (`Unnamed: 0`, …), the file is rejected with "no header row detected"; any other violation gets its own specific error.

Every validation error is shown in the UI as a short, plain-language message. No database is created for a file that fails validation.

**SQL safety (read-only)**

- The database is opened with a read-only SQLite connection (`mode=ro`).
- Only one statement is allowed, and it must start with `SELECT` or `WITH`. Statements that change data or the schema, `ATTACH`, and `PRAGMA` are rejected.
- Results are cut to the row limit, and the agent is told when this happens.
- SQL errors go back to the agent as tool output, so it can fix the query once or explain the problem. They do not crash the app.

**Answering errors**

- If a column or metric the user asks about doesn't exist, the agent says so and lists the columns that are available.
- If the agent can't get a result, it says it doesn't know instead of guessing.

**Session end and cleanup (local demo)**

A session ends when any of these happens:

1. The user clicks **End session**.
2. The user uploads a new file. Only the data is replaced; the chat is reset too.
3. The session is **inactive for a set time** (for example 30 minutes). This is checked each time the app reruns.

When a session ends, the app deletes the session's temporary folder (uploaded file and `data.db`) and clears its chat and agent state. Closing a browser tab can't be detected reliably, so **when the app starts it also deletes any leftover session folders** under the app's temp directory. This guarantees nothing is left behind between runs.

## 6. Acceptance Criteria

The demo works when all of these are true:

- [ ] A valid CSV and a valid single-sheet XLSX each upload and show the correct row count, column count, and preview.
- [ ] Uploading a multi-sheet XLSX loads only the first sheet and lists the ignored sheets.
- [ ] Unsupported, empty, unreadable, and headerless, numeric-header, and duplicate-header files each show a clear, specific error, and no database is created.
- [ ] "How many records are there?" returns the correct count.
- [ ] "What is the average sales amount?" returns the correct value for a sample sales file.
- [ ] "Which category has the highest total revenue?" returns the correct category, using a SQL aggregation.
- [ ] A question about data that isn't in the file (for example "What is the profit margin?" with no cost column) gets an explanation of what is missing, not a made-up number.
- [ ] A request that tries to change data (for example "delete all rows") is refused by the SQL tool, and the data is unchanged.
- [ ] Each answer says which tool or query produced it.
- [ ] After **End session**, uploading a new file, or a timeout, the session's temp folder is gone. Restarting the app deletes any leftover folders.
- [ ] The whole app runs locally with one command (`streamlit run app.py`) and one ADK agent.

## 7. Future Backlog

- Hosting as a microservice (separate API and UI)
- Cloud Run deployment using ADK's built-in deployment support
- Stronger guardrails (input and output filtering, query cost limits, PII handling)
- Richer data validation and type cleanup (dates, currencies, mixed types)
- SQL review and reflection (self-check or a critic step)
- Explicit planning step before running a query




---

### Assumptions

- The model is Gemini (for example `gemini-3.5-flash-lite`), used through ADK with a local API key or Vertex AI credentials.
- It is a single-user local demo, so in-memory session state is enough.
- Example questions use a sample sales dataset with columns such as `category`, `sales_amount`, and `revenue`.
