"""Tests for evals.hybridqa.prepare with a mini released_data (local fetch, fake embeddings, no network)."""

import csv
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
import yaml

from data_agent.config import load_config
from data_agent.events import TurnResult
from evals.dataset import EvalDataset, load_dataset
from evals.hybridqa import PrepareError, convert_table, entity_title, main, prepare, repair_headers, safe_file_stem
from evals.run import _load_prebuilt, run_eval


def table_json(name, rows, links):
    """A tables_tok file: header [Player, Team] and rows whose first cell links to an entity."""
    return {
        "url": f"https://en.wikipedia.org/wiki/{name}",
        "title": name,
        "header": [["Player", []], ["Team", []]],
        "data": [[[player, [link]], [team, []]] for (player, team), link in zip(rows, links)],
    }


# table id -> (rows, links); entity names include a Windows-unsafe one ("AC/DC: Live?")
TABLES = {
    "Tab_A_0": ([("Ann", "Reds"), ("Bob", "Blues")], ["/wiki/Ann_Lee", "/wiki/AC/DC:_Live%3F"]),
    "Tab_B_1": ([("Cid", "Greens")], ["/wiki/Cid"]),
    "Tab_C_2": ([("Dee", "Golds")], ["/wiki/Dee"]),
}
PASSAGES = {
    "/wiki/Ann_Lee": "Ann Lee was born in Paris and plays for the Reds.",
    "/wiki/AC/DC:_Live%3F": "A live album recorded in 1991.",
    "/wiki/Cid": "Cid was born in Rome.",
    "/wiki/Dee": "Dee was born in Oslo.",
}

# (qid, table, type, answer)
QUESTIONS = [
    ("qa1", "Tab_A_0", "table", "Reds"),
    ("qa2", "Tab_A_0", "passage", "Paris"),
    ("qb1", "Tab_B_1", "table", "Greens"),
    ("qb2", "Tab_B_1", "passage", "Rome"),
    ("qc1", "Tab_C_2", "table", "Golds"),
    ("qc2", "Tab_C_2", "passage", "Oslo"),
    ("qm1", "Tab_M_9", "table", "Nowhere"),  # its table cannot be fetched
    ("qx1", "Tab_A_0", None, "Unlabeled"),  # in neither list
]


@pytest.fixture
def hybridqa(tmp_path) -> Path:
    root = tmp_path / "HybridQA"
    released = root / "released_data"
    released.mkdir(parents=True)
    traced = [
        {
            "question_id": qid, "question": f"Question {qid}?", "table_id": tid, "answer-text": ans,
            "answer-node": [[ans, [0, 1], "/wiki/Ann_Lee", "passage"], ["x", [1, 1], "/wiki/AC/DC:_Live%3F", "table"]]
            if kind == "passage" else [[ans, [0, 1], None, "table"]],
        }
        for qid, tid, kind, ans in QUESTIONS
    ]
    (released / "dev.traced.json").write_text(json.dumps(traced), encoding="utf-8")
    reference = {
        "reference": {qid: ans for qid, _, _, ans in QUESTIONS},
        "table": [q for q, _, k, _ in QUESTIONS if k == "table"],
        "passage": [q for q, _, k, _ in QUESTIONS if k == "passage"],
    }
    (released / "dev_reference.json").write_text(json.dumps(reference), encoding="utf-8")
    return root


def local_fetch(rel: str) -> bytes:
    kind, name = rel.split("/")
    table_id = name.removesuffix(".json")
    if table_id not in TABLES:
        raise FileNotFoundError(f"404: {rel}")
    rows, links = TABLES[table_id]
    if kind == "tables_tok":
        return json.dumps(table_json(table_id, rows, links)).encode()
    return json.dumps({k: v for k, v in PASSAGES.items() if k in links}).encode()


@pytest.fixture
def config(tmp_path) -> Path:
    path = tmp_path / "run.yaml"
    path.write_text(
        yaml.safe_dump({"name": "h", "agent": {"chunking": {"chunk_words": 60}}, "dataset": {"path": "x.yaml"}}), encoding="utf-8"
    )
    return path


def build(hybridqa, config, out, fake_embed, per_type=1, **kw):
    return prepare(hybridqa, config, out, per_type=per_type, seed=7, cache=out.parent / "raw", fetch=local_fetch, **kw)


def case_ids(out):
    return [c.id for c in load_dataset(out / "dataset.yaml").cases]


def test_dataset_validates_with_the_requested_counts_per_type(hybridqa, config, tmp_path, fake_embed):
    out = tmp_path / "b"
    info = build(hybridqa, config, out, fake_embed, per_type=1)
    ds = load_dataset(out / "dataset.yaml")
    assert isinstance(ds, EvalDataset) and ds.name == "hybridqa_dev_s2"
    assert info["sample"]["cases_per_type"] == {"table": 1, "passage": 1}
    for c in ds.cases:
        assert c.tags[0] == "hybridqa" and c.tags[1] in ("table", "passage")
        assert c.state_file is not None and (out / c.state_file).is_file()
        assert c.expected.answer_contains == c.expected.reference_answers and len(c.expected.reference_answers) == 1
    passage = next(c for c in ds.cases if "passage" in c.tags)
    assert passage.expected.object_ids == ["Ann Lee"]  # the passage node; the table node is not a document
    assert next(c for c in ds.cases if "table" in c.tags).expected.object_ids == []


def test_same_seed_same_cases_and_a_larger_sample_contains_the_smaller(hybridqa, config, tmp_path, fake_embed):
    small = case_ids(_built(hybridqa, config, tmp_path / "s", fake_embed, 1))
    again = case_ids(_built(hybridqa, config, tmp_path / "s2", fake_embed, 1))
    large = case_ids(_built(hybridqa, config, tmp_path / "l", fake_embed, 3))
    assert small == again
    assert set(small) <= set(large) and len(large) > len(small)


def _built(hybridqa, config, out, fake_embed, per_type, **kw):
    build(hybridqa, config, out, fake_embed, per_type=per_type, **kw)
    return out


def test_unlabeled_are_excluded_and_a_failed_fetch_is_skipped_and_recorded(hybridqa, config, tmp_path, fake_embed):
    out = tmp_path / "b"
    info = build(hybridqa, config, out, fake_embed, per_type=None)
    assert "qx1" not in case_ids(out) and info["sample"]["unlabeled_excluded"] == 1
    skipped = {s["qid"]: s["reason"] for s in info["sample"]["skipped"]}
    assert "qm1" in skipped and "404" in skipped["qm1"]
    assert "qm1" not in case_ids(out)
    assert len(case_ids(out)) == 3  # one question per table with --one-per-table
    saved = json.loads((out / "prepare.json").read_text(encoding="utf-8"))
    assert saved["sample"]["skipped"] == info["sample"]["skipped"] and saved["sources"]["raw_files"]


def test_no_one_per_table_builds_a_shared_table_once(hybridqa, config, tmp_path, fake_embed):
    out = tmp_path / "b"
    info = build(hybridqa, config, out, fake_embed, per_type=None, one_per_table=False)
    assert len(case_ids(out)) == 6 and info["counts"]["tables"] == 3
    assert len(list((out / "tables").iterdir())) == 3


def test_csv_and_entity_files_match_the_fixture(hybridqa, config, tmp_path, fake_embed):
    out = tmp_path / "b"
    build(hybridqa, config, out, fake_embed, per_type=None, one_per_table=False)
    folder = out / "tables" / "tab_a_0"
    rows = list(csv.reader((folder / "tab_a_0.csv").read_text(encoding="utf-8").splitlines()))
    assert rows == [["Player", "Team"], ["Ann", "Reds"], ["Bob", "Blues"]]  # links dropped
    ann = (folder / "docs" / "Ann Lee.md").read_text(encoding="utf-8")
    assert ann.startswith("# Ann Lee\n\nAnn Lee was born in Paris")


def test_a_windows_unsafe_entity_name_becomes_a_safe_file_name(hybridqa, config, tmp_path, fake_embed):
    out = tmp_path / "b"
    build(hybridqa, config, out, fake_embed, per_type=None, one_per_table=False)
    names = sorted(p.name for p in (out / "tables" / "tab_a_0" / "docs").iterdir())
    assert names == ["AC_DC_ Live_.md", "Ann Lee.md"]
    text = (out / "tables" / "tab_a_0" / "docs" / "AC_DC_ Live_.md").read_text(encoding="utf-8")
    assert text.startswith("# AC/DC: Live?\n")  # the title keeps the real name
    assert safe_file_stem("CON") == "_CON" and safe_file_stem("a: b?") == "a_ b_"
    taken: set[str] = set()
    assert [safe_file_stem("A/B", taken), safe_file_stem("A:B", taken)] == ["A_B", "A_B_2"]


def test_state_paths_are_relative_and_load_through_the_runner(hybridqa, config, tmp_path, fake_embed):
    out = tmp_path / "b"
    build(hybridqa, config, out, fake_embed, per_type=1)
    case = load_dataset(out / "dataset.yaml").cases[0]
    state_path = out / case.state_file
    raw = json.loads(state_path.read_text(encoding="utf-8"))
    assert raw["db_path"] == "data.db" and raw["docs_db_path"] == "docs.db"
    assert set(raw) == {"db_path", "column_map", "schema_summary", "docs_summary", "docs_db_path"}
    cfg = load_config(config)
    state, info = _load_prebuilt(state_path, cfg, fallback_dir=out)
    assert Path(state["db_path"]).is_absolute() and Path(state["docs_db_path"]).is_file()
    assert info["chunking"]["chunk_words"] == 60
    with closing(sqlite3.connect(state["db_path"])) as conn:
        [(table,)] = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] >= 1
    assert state_path.parent.name == table  # the folder, the CSV and the SQL table share one safe id


def test_reference_subset_has_exactly_the_sampled_qids(hybridqa, config, tmp_path, fake_embed):
    out = tmp_path / "b"
    build(hybridqa, config, out, fake_embed, per_type=1)
    ref = json.loads((out / "reference" / "dev_reference.json").read_text(encoding="utf-8"))
    ds = load_dataset(out / "dataset.yaml")
    by_type = {t: [c.id for c in ds.cases if t in c.tags] for t in ("table", "passage")}
    assert ref["table"] == by_type["table"] and ref["passage"] == by_type["passage"]
    assert set(ref["reference"]) == {c.id for c in ds.cases}
    assert all(ref["reference"][c.id] == c.expected.reference_answers[0] for c in ds.cases)


def test_prepare_json_records_sources_counts_and_settings(hybridqa, config, tmp_path, fake_embed):
    out = tmp_path / "b"
    build(hybridqa, config, out, fake_embed, per_type=1)
    info = json.loads((out / "prepare.json").read_text(encoding="utf-8"))
    assert set(info["sources"]["released_data"]) == {"dev.traced.json", "dev_reference.json"}
    assert info["sample"]["seed"] == 7 and info["counts"]["cases"] == 2 and info["counts"]["chunks"] >= 2
    assert info["chunking"]["chunk_words"] == 60 and info["embedding"]["dim"] == 768
    assert (tmp_path / "raw" / "tables_tok").is_dir()  # the raw files are cached for the next sample


def test_cached_files_are_not_fetched_again(hybridqa, config, tmp_path, fake_embed):
    calls = []

    def counting(rel):
        calls.append(rel)
        return local_fetch(rel)

    out = tmp_path / "b"
    prepare(hybridqa, config, out, per_type=1, seed=7, cache=tmp_path / "raw", fetch=counting)
    first = [c for c in calls if "Tab_M_9" not in c]  # a failed download is not cached, so it is retried
    prepare(hybridqa, config, tmp_path / "b2", per_type=1, seed=7, cache=tmp_path / "raw", fetch=counting)
    assert first and [c for c in calls if "Tab_M_9" not in c] == first  # the second sample reads the rest from the cache


def test_run_eval_on_the_bundle_gives_per_tag_means(hybridqa, config, tmp_path, fake_embed):
    out = tmp_path / "b"
    build(hybridqa, config, out, fake_embed, per_type=1)
    cfg_path = tmp_path / "bundle_run.yaml"
    cfg_path.write_text(
        yaml.safe_dump({
            "name": "h", "agent": {"chunking": {"chunk_words": 60}}, "dataset": {"path": str(out / "dataset.yaml")},
            "eval": {"metrics": ["exact_match", "squad_f1", "coverage"], "predictions": True, "prediction_answer": "first_line"},
        }),
        encoding="utf-8",
    )
    expected = {c.id: c.expected.reference_answers[0] for c in load_dataset(out / "dataset.yaml").cases}

    class Echo:
        def __init__(self, cfg, state):
            self.state = state

        def ask(self, question):
            qid = question.removeprefix("Question ").removesuffix("?")
            return TurnResult(f"{expected[qid]}\nbecause.", [], None, 0.1)

        def close(self, delete_files=True):
            assert delete_files is False

    metrics = run_eval(load_config(cfg_path), tmp_path / "run", session_factory=Echo)
    assert {t: metrics["by_tag"][t]["n"] for t in ("table", "passage", "hybridqa")} == {"table": 1, "passage": 1, "hybridqa": 2}
    assert metrics["by_tag"]["table"]["metrics"]["exact_match"] == 1.0
    assert metrics["by_tag"]["passage"]["metrics"]["squad_f1"] == 1.0
    assert metrics["data"]["counts"]["cases"] == 2


def test_missing_label_files_fail_clearly(config, tmp_path):
    with pytest.raises(PrepareError, match="dev.traced.json"):
        prepare(tmp_path / "nope", config, tmp_path / "b", fetch=local_fetch)


def test_cli_prepare_prints_the_counts(hybridqa, config, tmp_path, fake_embed, monkeypatch, capsys):
    import evals.hybridqa as mod

    monkeypatch.setattr(mod, "http_fetch", local_fetch)
    out = tmp_path / "b"
    assert main(["prepare", "--hybridqa", str(hybridqa), "--config", str(config), "--out", str(out),
                 "--per-type", "1", "--cache", str(tmp_path / "raw"), "--no-one-per-table"]) == 0
    assert '"cases": 2' in capsys.readouterr().out
    assert main(["prepare", "--hybridqa", str(tmp_path / "none"), "--config", str(config), "--out", str(out)]) == 1


def test_helpers():
    assert entity_title("/wiki/Emmitt_Smith") == "Emmitt Smith"
    assert entity_title("/wiki/Caf%C3%A9_(film)#History") == "Café (film)"
    assert repair_headers(["Year", "", "1990", "Year", "year"]) == ["Year", "col_2", "col_1990", "Year 2", "year 3"]
    data = convert_table("T", table_json("T", [("A", "B")], ["/wiki/A"]), {"/wiki/A": "text"}, "t")
    assert list(data.docs) == ["A.md"]
    with pytest.raises(ValueError, match="no linked passage"):
        convert_table("T", table_json("T", [("A", "B")], ["/wiki/A"]), {}, "t")


def test_a_failed_embedding_is_retried_and_then_fails_clearly(hybridqa, config, tmp_path, fake_embed, monkeypatch):
    import evals.hybridqa as mod
    from data_agent import doc_ingest

    monkeypatch.setattr(mod, "EMBED_WAIT_S", 0)
    real, calls = doc_ingest.embed, []

    def flaky(texts, task_type):
        calls.append(1)
        return None if len(calls) == 1 else real(texts, task_type)  # first call is rate-limited

    monkeypatch.setattr(doc_ingest, "embed", flaky)
    build(hybridqa, config, tmp_path / "ok", fake_embed, per_type=1)
    assert len(calls) > 2  # the failed table was built again

    monkeypatch.setattr(doc_ingest, "embed", lambda texts, task_type: None)
    with pytest.raises(PrepareError, match="embedding failed"):
        build(hybridqa, config, tmp_path / "bad", fake_embed, per_type=1)


def test_dataset_state_files_use_forward_slashes(hybridqa, config, tmp_path, fake_embed):
    out = tmp_path / "b"
    build(hybridqa, config, out, fake_embed, per_type=1)
    raw = yaml.safe_load((out / "dataset.yaml").read_text(encoding="utf-8"))
    assert all("\\" not in c["state_file"] and c["state_file"].startswith("tables/") for c in raw["cases"])


def test_prepare_with_an_ollama_config_records_the_provider(hybridqa, tmp_path, monkeypatch):
    import numpy as np

    from data_agent import doc_ingest

    seen = []

    def embed(texts, task_type, embedding=None):
        seen.append(embedding)
        vecs = np.zeros((len(texts), 768), dtype=np.float32)
        vecs[:, 0] = 1.0
        return vecs

    monkeypatch.setattr(doc_ingest, "embed", embed)
    path = tmp_path / "ollama.yaml"
    path.write_text(
        yaml.safe_dump({"name": "h", "agent": {"embedding": {"provider": "ollama", "model": "nomic-embed-text:latest"}},
                        "dataset": {"path": "x.yaml"}}),
        encoding="utf-8",
    )
    out = tmp_path / "b"
    build(hybridqa, path, out, None, per_type=1)
    info = json.loads((out / "prepare.json").read_text(encoding="utf-8"))
    assert info["embedding"]["provider"] == "ollama" and info["embedding"]["model"] == "nomic-embed-text:latest"
    assert seen and all(e.provider == "ollama" for e in seen)  # the provider reaches the embedding call
