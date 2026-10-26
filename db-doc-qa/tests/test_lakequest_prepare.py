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
from evals.lakequest import PrepareError, _as_list, main, normalise_policy, prepare

POLICIES = {
    "alpha-policy": ("Alpha Policy", "## 1) Purpose\nKeep records safe.\n\n## 5) Required Safeguards\nVerify identity before any change."),
    "beta-policy": ("Beta Policy", "## 1) Purpose\nHandle cheques.\n\n## 2) Scope\nApplies to all branches."),
}


def _lake_db(path: Path) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('CREATE TABLE client (client_id INTEGER PRIMARY KEY, name TEXT)')
        conn.execute('CREATE TABLE "transaction" (txn_id INTEGER, client_id INTEGER, amount REAL)')
        conn.execute('CREATE INDEX idx_txn_client ON "transaction"(client_id)')
        conn.execute("CREATE TABLE persona_workflows (qa_id TEXT, question TEXT, answer_direct TEXT)")  # the leak
        conn.executemany("INSERT INTO client VALUES (?, ?)", [(1, "Ann"), (2, "Bob")])
        conn.executemany('INSERT INTO "transaction" VALUES (?, ?, ?)', [(1, 1, 9.5), (2, 2, 3.0), (3, 1, 1.0)])
        conn.execute("INSERT INTO persona_workflows VALUES ('q1', 'secret?', 'the answer')")
        conn.commit()


@pytest.fixture
def lake_zip(tmp_path) -> Path:
    db = tmp_path / "financial_data_lake.db"
    _lake_db(db)
    corpus = pd.DataFrame(
        {
            "domain": "bank",
            "object_id": ["client", "transaction", *POLICIES],
            "object_type": ["table", "table", "policy_document", "policy_document"],
            "modality": ["table", "table", "text", "text"],
            "license_tier": "open",
        }
    )
    buf = io.BytesIO()
    corpus.to_parquet(buf)
    rows = [{"object_id": k, "object_type": "policy_document", "title": t, "summary": "s", "document": d} for k, (t, d) in POLICIES.items()]
    rows.append({"object_id": "not-in-corpus", "object_type": "policy_document", "title": "X", "summary": "s", "document": "## 1) X\nx"})
    path = tmp_path / "lake.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("lakes/bank/corpus_objects.parquet", buf.getvalue())
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
    assert _tables(out / "bank.db") == {"client", "transaction"}  # persona_workflows is not copied
    with closing(sqlite3.connect(out / "bank.db")) as conn:
        assert conn.execute('SELECT COUNT(*) FROM "transaction"').fetchone()[0] == 3
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'idx_txn_client'").fetchone()
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert {"idx_client_client_id", "idx_transaction_client_id", "idx_transaction_txn_id"} <= names
        assert not any("name" in n or "amount" in n for n in names)  # only the *_id columns
        plan = " ".join(str(r) for r in conn.execute('EXPLAIN QUERY PLAN SELECT * FROM "transaction" WHERE client_id = 1'))
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
    assert '"transaction" (3 rows): txn_id INT, client_id INT, amount REAL' in state["schema_summary"]
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
    assert info["counts"]["cases"] == 2 and info["counts"]["tables"] == {"client": 2, "transaction": 3}
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
