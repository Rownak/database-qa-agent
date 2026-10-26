"""Tests for evals.lakequest.prepare with a tiny fixture lake (embeddings mocked; no network)."""

import io
import json
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path

import pandas as pd
import pytest

from evals.dataset import EvalDataset, load_dataset
from evals.lakequest import PrepareError, _as_list, main, normalise_policy, prepare, referenced_sections

POLICIES = {
    "alpha-policy": ("Alpha Policy", "## 1) Purpose\nKeep records safe.\n\n## 5) Required Safeguards\nVerify identity before any change."),
    "beta-policy": ("Beta Policy", "## 1) Purpose\nHandle cheques.\n\n## 2) Scope\nApplies to all branches."),
}


def _lake_db(path: Path) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE client (client_id INTEGER PRIMARY KEY, first_name TEXT, last_name TEXT)")
        conn.execute("CREATE TABLE bank_account (account_id TEXT, client_id INTEGER)")
        conn.execute('CREATE TABLE "transaction" (txn_id INTEGER, account_id TEXT, amount REAL)')
        conn.execute("CREATE TABLE cheque (cheque_id TEXT, account_id TEXT)")
        conn.execute('CREATE INDEX idx_txn_account ON "transaction"(account_id)')
        conn.execute(
            "CREATE TABLE persona_workflows (qa_id TEXT, client_id INTEGER, policy_id TEXT, question TEXT, "
            "answer_direct TEXT, answer_reasoning TEXT, sources TEXT)"
        )  # the leak
        conn.executemany("INSERT INTO client VALUES (?, ?, ?)", [(1, "Ann", "Lee"), (2, "Bob", "Ray")])
        conn.executemany("INSERT INTO bank_account VALUES (?, ?)", [("a1", 1), ("a2", 1), ("a3", 2)])
        conn.executemany('INSERT INTO "transaction" VALUES (?, ?, ?)', [(1, "a1", 9.5), (2, "a3", 3.0), (3, "a2", 1.0)])
        conn.execute("INSERT INTO cheque VALUES ('ch1', 'a1')")
        conn.executemany(
            "INSERT INTO persona_workflows VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("qa-2", 1, "beta-policy", "Ann Lee (client_id: 1): may we proceed?", "Yes.", "Sections 1 and 2 allow it, see Section 9.", '["client", "cheque"]'),
                ("qa-1", 1, "alpha-policy", "Ann Lee (client_id: 1): is a change allowed?", "No.", "Section 5 requires identity checks.", '["client", "bank_account"]'),
                ("qa-3", 2, "alpha-policy", "Bob Ray (client_id: 2): test-split question", "No.", "Section 5.", '["client"]'),
            ],
        )
        conn.commit()


@pytest.fixture
def lake_zip(tmp_path) -> Path:
    db = tmp_path / "financial_data_lake.db"
    _lake_db(db)
    corpus = pd.DataFrame(
        {
            "domain": "bank",
            "object_id": ["client", "bank_account", "transaction", "cheque", *POLICIES],
            "object_type": ["table"] * 4 + ["policy_document"] * 2,
            "modality": ["table"] * 4 + ["text"] * 2,
            "license_tier": "open",
        }
    )
    buf = io.BytesIO()
    corpus.to_parquet(buf)
    split_buf = io.BytesIO()
    pd.DataFrame({"domain": "bank", "entity_id": ["client:1", "client:2"], "split": ["validation", "test"]}).to_parquet(split_buf)
    rows = [{"object_id": k, "object_type": "policy_document", "title": t, "summary": "s", "document": d} for k, (t, d) in POLICIES.items()]
    rows.append({"object_id": "not-in-corpus", "object_type": "policy_document", "title": "X", "summary": "s", "document": "## 1) X\nx"})
    path = tmp_path / "lake.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("lakes/bank/corpus_objects.parquet", buf.getvalue())
        z.writestr("lakes/bank/split_entities.parquet", split_buf.getvalue())
        z.write(db, "raw_lake_snapshot/lakes/bank/structured/financial_data_lake.db")
        z.writestr("raw_lake_snapshot/lakes/bank/policy_documents.jsonl", "\n".join(json.dumps(r) for r in rows))
    return path


@pytest.fixture
def questions(tmp_path) -> Path:
    folder = tmp_path / "input"
    folder.mkdir()
    pd.DataFrame(
        {
            "qa_id": ["q1", "q2", "q3"],
            "question": ["What is Ann's balance?", "Can Bob close it per alpha-policy?", "A drug question"],
            "domain": ["bank", "bank", "drug"],
        }
    ).to_parquet(folder / "questions.parquet")
    return folder


@pytest.fixture
def references(tmp_path) -> Path:
    folder = tmp_path / "reference"
    folder.mkdir()
    pd.DataFrame(
        {
            "qa_id": ["q1", "q2", "q3"],
            "domain": ["bank", "bank", "drug"],
            "answer_short": ["Ten", "", "x"],
            "answer_long": ["Ann has ten", "Yes after checks", "y"],
            "gold_object_ids": [["client", "transaction"], ["alpha-policy", "client"], ["drug1"]],
        }
    ).to_parquet(folder / "references.parquet")
    return folder


@pytest.fixture
def config(tmp_path) -> Path:
    path = tmp_path / "run.yaml"
    path.write_text(
        "name: t\nagent:\n  chunking: {chunk_words: 60}\ndataset: {path: x.yaml}\n", encoding="utf-8"
    )
    return path


def _tables(db: Path) -> set[str]:
    with closing(sqlite3.connect(db)) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_bank_db_has_only_the_corpus_tables(lake_zip, questions, references, config, tmp_path, fake_embed):
    out = tmp_path / "out"
    prepare(lake_zip, questions, references, "bank", config, out)
    assert _tables(out / "bank.db") == {"client", "bank_account", "transaction", "cheque"}  # persona_workflows is not copied
    with closing(sqlite3.connect(out / "bank.db")) as conn:
        assert conn.execute('SELECT COUNT(*) FROM "transaction"').fetchone()[0] == 3
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'idx_txn_account'").fetchone()
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert {"idx_client_client_id", "idx_bank_account_account_id", "idx_transaction_account_id", "idx_cheque_account_id"} <= names
        assert not any("name" in n or "amount" in n for n in names)  # only the *_id columns
        plan = " ".join(str(r) for r in conn.execute('EXPLAIN QUERY PLAN SELECT * FROM "transaction" WHERE account_id = ?', ("a1",)))
        assert "USING INDEX" in plan or "USING COVERING INDEX" in plan


def test_a_missing_whitelisted_table_stops_the_run(lake_zip, questions, config, tmp_path, fake_embed):
    broken = tmp_path / "broken.zip"
    db = tmp_path / "no_client.db"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute('CREATE TABLE "transaction" (txn_id INTEGER)')
        conn.commit()
    with zipfile.ZipFile(lake_zip) as src, zipfile.ZipFile(broken, "w") as dst:
        for item in src.namelist():
            if not item.endswith(".db"):
                dst.writestr(item, src.read(item))
        dst.write(db, "raw_lake_snapshot/lakes/bank/structured/financial_data_lake.db")
    with pytest.raises(PrepareError, match="client"):
        prepare(broken, questions, None, "bank", config, tmp_path / "out")


def test_policies_are_normalised_and_indexed(lake_zip, questions, config, tmp_path, fake_embed):
    out = tmp_path / "out"
    info = prepare(lake_zip, questions, None, "bank", config, out)
    assert info["counts"]["policies"] == 2 and info["counts"]["policies_skipped"] == 0
    with closing(sqlite3.connect(out / "docs.db")) as conn:
        sources = {r[0] for r in conn.execute("SELECT DISTINCT source FROM chunks")}
        numbers = [r[0] for r in conn.execute("SELECT section_number FROM chunks WHERE source = 'alpha-policy.md'")]
    assert sources == {"alpha-policy.md", "beta-policy.md"}  # "not-in-corpus" is not a corpus object
    assert "5" in numbers  # "## 5) Required Safeguards" became a numbered section


def test_normalise_policy():
    assert normalise_policy(" T ", "## 5) Safeguards\nx\n### 2.1) Sub") == "# T\n\n## 5 Safeguards\nx\n### 2.1 Sub\n"


def test_state_json_reloads_and_its_paths_exist(lake_zip, questions, config, tmp_path, fake_embed):
    out = tmp_path / "out"
    prepare(lake_zip, questions, None, "bank", config, out)
    state = json.loads((out / "state.json").read_text(encoding="utf-8"))
    assert state["column_map"] == {}
    assert (out / state["db_path"]).is_file() and (out / state["docs_db_path"]).is_file()
    assert '"transaction" (3 rows): txn_id INT, account_id TEXT, amount REAL' in state["schema_summary"]
    assert "persona_workflows" not in state["schema_summary"]
    assert state["docs_summary"].splitlines() == ["alpha-policy.md — Alpha Policy", "beta-policy.md — Beta Policy"]


def test_dataset_validates_and_keeps_one_domain(lake_zip, questions, references, config, tmp_path, fake_embed):
    out = tmp_path / "out"
    prepare(lake_zip, questions, references, "bank", config, out)
    ds = load_dataset(out / "dataset.yaml")
    assert isinstance(ds, EvalDataset) and ds.state_file == Path("state.json")
    assert [c.id for c in ds.cases] == ["q1", "q2"]  # the drug question is filtered out
    q1, q2 = ds.cases
    assert q1.tags == ["bank"] and q1.expected.reference_answers == ["Ten", "Ann has ten"]
    assert q1.expected.object_ids == ["client", "transaction"]
    assert q2.expected.reference_answers == ["Yes after checks"]  # the empty answer_short is left out
    assert q2.expected.object_ids == ["alpha-policy", "client"]  


def test_without_references_every_expected_is_empty(lake_zip, questions, config, tmp_path, fake_embed):
    out = tmp_path / "out"
    prepare(lake_zip, questions, None, "bank", config, out)
    for case in load_dataset(out / "dataset.yaml").cases:
        assert case.expected.reference_answers == [] and case.expected.object_ids == [] and case.expected.tools is None


def test_prepare_json_records_hashes_counts_and_settings(lake_zip, questions, references, config, tmp_path, fake_embed):
    out = tmp_path / "out"
    prepare(lake_zip, questions, references, "bank", config, out)
    info = json.loads((out / "prepare.json").read_text(encoding="utf-8"))
    assert len(info["sources"]["lake_zip"]["sha256"]) == 64 and info["sources"]["references"]["file"] == "references.parquet"
    assert info["counts"]["cases"] == 2 and info["counts"]["tables"] == {"bank_account": 3, "cheque": 1, "client": 2, "transaction": 3}
    assert info["chunking"]["chunk_words"] == 60 and info["embedding"]["dim"] == 768


def test_a_missing_question_column_is_a_clear_error(lake_zip, config, tmp_path, fake_embed):
    folder = tmp_path / "bad"
    folder.mkdir()
    pd.DataFrame({"qa_id": ["q1"], "question": ["?"]}).to_parquet(folder / "questions.parquet")
    with pytest.raises(PrepareError, match="domain"):
        prepare(lake_zip, folder, None, "bank", config, tmp_path / "out")


def test_cli_reports_errors_without_a_traceback(lake_zip, config, tmp_path, capsys):
    code = main(["prepare", "--lake-zip", str(lake_zip), "--questions", str(tmp_path / "none"), "--config", str(config), "--out", str(tmp_path / "o")])
    assert code == 1 and "error:" in capsys.readouterr().err


def test_list_cells_are_read_like_the_official_scorer():
    assert _as_list('["a", "b"]') == ["a", "b"] and _as_list("a, b") == ["a", "b"] and _as_list(None) == []
    assert _as_list(float("nan")) == [] and _as_list("a") == ["a"] and _as_list(("a", " ")) == ["a"]


# ---- bank_sample (no Codabench files) ----

SMALL = {"hybrid": 2, "table": 3, "document": 2}


def _sample(lake_zip, config, tmp_path, name="sample", seed=1):
    out = tmp_path / name
    info = prepare(lake_zip, None, None, "bank", config, out, seed=seed, counts=SMALL)
    return out, info


def test_sample_cases_validate_and_have_the_expected_counts(lake_zip, config, tmp_path, fake_embed):
    out, info = _sample(lake_zip, config, tmp_path)
    ds = load_dataset(out / "dataset.yaml")
    assert ds.state_file == Path("state.json")
    by_type = {t: [c for c in ds.cases if t in c.tags] for t in SMALL}
    assert {t: len(v) for t, v in by_type.items()} == SMALL == info["counts"]["cases_per_type"]
    assert all(c.tags[0] == "bank" for c in ds.cases)
    assert [c.id for c in by_type["hybrid"]] == ["qa-1", "qa-2"]  # sorted by qa_id; the test-split client (qa-3) is left out
    assert info["sample"] == {"seed": 1, "counts": SMALL}


def test_sample_hybrid_case_fields(lake_zip, config, tmp_path, fake_embed):
    out, _ = _sample(lake_zip, config, tmp_path)
    qa1, qa2 = load_dataset(out / "dataset.yaml").cases[:2]
    assert qa1.expected.tools == ["run_sql", "search_documents"]
    assert [(x.source, x.section) for x in qa1.expected.sections] == [("alpha-policy.md", "5")]
    assert qa1.expected.reference_answers == ["No.", "Section 5 requires identity checks."]
    assert qa1.expected.object_ids == ["client", "bank_account", "alpha-policy"]
    assert [x.section for x in qa2.expected.sections] == ["1", "2"]  # "Section 9" does not exist in the policy


def test_sample_table_answers_equal_the_sql_count(lake_zip, config, tmp_path, fake_embed):
    out, _ = _sample(lake_zip, config, tmp_path)
    cases = [c for c in load_dataset(out / "dataset.yaml").cases if "table" in c.tags]
    answers = {c.question.split(" does ")[0]: c.expected.answer_contains[0] for c in cases}
    assert answers == {"How many accounts": "2", "How many transactions": "2", "How many cheques": "1"}  # Ann Lee, client 1
    assert all("Ann Lee (client_id: 1)" in c.question and c.expected.tools == ["run_sql"] for c in cases)
    assert all(c.expected.reference_answers == c.expected.answer_contains for c in cases)
    cheque = next(c for c in cases if "cheques" in c.question)
    assert cheque.expected.object_ids == ["client", "bank_account", "cheque"]


def test_sample_document_cases_match_the_policy_headings(lake_zip, config, tmp_path, fake_embed):
    out, _ = _sample(lake_zip, config, tmp_path)
    section_q, purpose_q = [c for c in load_dataset(out / "dataset.yaml").cases if "document" in c.tags]
    headings = {"alpha-policy": ("Required Safeguards", "5"), "beta-policy": ("Scope", "2")}
    policy_id = section_q.expected.sections[0].source.removesuffix(".md")
    title, number = headings[policy_id]
    assert section_q.question == f"Under policy {policy_id}, which section covers {title}?"
    assert section_q.expected.answer_contains == [number] == [section_q.expected.sections[0].section]
    assert section_q.expected.reference_answers == [number]  # answer_f1 sees it, like the official scorer does
    assert purpose_q.question.startswith("What is the purpose of policy ")
    purpose_id = purpose_q.expected.object_ids[0]
    assert purpose_q.expected.reference_answers == [{"alpha-policy": "Keep records safe.", "beta-policy": "Handle cheques."}[purpose_id]]
    assert purpose_q.expected.tools == ["search_documents"] and purpose_q.expected.sections[0].section == "1"


def test_sample_keeps_the_leak_table_out_of_bank_db(lake_zip, config, tmp_path, fake_embed):
    out, _ = _sample(lake_zip, config, tmp_path)
    assert "persona_workflows" not in _tables(out / "bank.db")
    assert "persona_workflows" not in json.loads((out / "state.json").read_text(encoding="utf-8"))["schema_summary"]


def test_sample_references_jsonl_has_the_scorer_columns(lake_zip, config, tmp_path, fake_embed):
    out, _ = _sample(lake_zip, config, tmp_path)
    rows = [json.loads(line) for line in (out / "reference" / "references.jsonl").read_text(encoding="utf-8").splitlines()]
    cases = load_dataset(out / "dataset.yaml").cases
    assert [r["qa_id"] for r in rows] == [c.id for c in cases]
    for row in rows:
        assert {"qa_id", "domain", "question", "answer_short", "answer_long", "gold_object_ids", "gold_provenance_ids"} <= row.keys()
        assert row["domain"] == "bank" and row["answer_short"] and row["gold_provenance_ids"] == []
    assert rows[0]["answer_short"] == "No." and rows[0]["answer_long"] == "Section 5 requires identity checks."


def test_sample_is_deterministic_for_a_seed(lake_zip, config, tmp_path, fake_embed):
    a, _ = _sample(lake_zip, config, tmp_path, "a", seed=7)
    b, _ = _sample(lake_zip, config, tmp_path, "b", seed=7)
    assert (a / "dataset.yaml").read_text(encoding="utf-8") == (b / "dataset.yaml").read_text(encoding="utf-8")


def test_sample_mode_errors(lake_zip, config, tmp_path, references, fake_embed):
    with pytest.raises(PrepareError, match="--questions"):
        prepare(lake_zip, None, references, "bank", config, tmp_path / "o")
    with pytest.raises(PrepareError, match="bank domain"):
        prepare(lake_zip, None, None, "drug", config, tmp_path / "o")


def test_referenced_sections():
    available = {"1": ("A", "a"), "2": ("B", "b"), "5": ("C", "c")}
    assert referenced_sections("Section 5 and Sections 1, 2 & 9; Section 5 again", available) == ["5", "1", "2"]
    assert referenced_sections("no reference", available) == []
