"""Tests for the HybridQA run configs, the instruction file and `evals.hybridqa score` (no network)."""

import json
import re

import pytest

from data_agent.agent import build_agent
from data_agent.config import AgentConfig, load_config
from evals.hybridqa import PrepareError, main, official_scores, score
from evals.metrics import METRICS
from evals.dataset import EvalCase, Expected
from data_agent.events import TurnResult

CONFIGS = ["configs/eval/hybridqa_dev50.yaml", "configs/eval/hybridqa_dev50_ollama.yaml"]
PROMPT = "configs/prompts/hybridqa.md"

# qid -> (type, gold, prediction, EM, F1)
PAIRS = {
    "t1": ("table", "The Beatles", "beatles", 1, 1.0),
    "t2": ("table", "New York City", "New York", 0, 0.8),
    "p1": ("passage", "Paris", "Berlin", 0, 0.0),
    "p2": ("passage", "1999", "1999.", 1, 1.0),
}


def make_run(tmp_path, drop=()):
    run, ref = tmp_path / "run", tmp_path / "dev_reference.json"
    run.mkdir()
    lines = [
        {"qa_id": q, "answer": pred, "object_ids": []} for q, (_, _, pred, _, _) in PAIRS.items() if q not in drop
    ]
    (run / "predictions.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    ref.write_text(
        json.dumps({
            "reference": {q: gold for q, (_, gold, *_) in PAIRS.items()},
            "table": [q for q, v in PAIRS.items() if v[0] == "table"],
            "passage": [q for q, v in PAIRS.items() if v[0] == "passage"],
        }),
        encoding="utf-8",
    )
    return run, ref


@pytest.mark.parametrize("path", CONFIGS)
def test_hybridqa_configs_load(path):
    cfg = load_config(path)
    assert cfg.agent.instruction_file.as_posix() == PROMPT and cfg.agent.intent_enabled is False
    assert cfg.agent.model.temperature == 0.0
    assert cfg.eval.metrics == ["exact_match", "squad_f1", "answer_contains", "coverage", "object_f1"]
    assert cfg.eval.predictions is True and cfg.eval.prediction_answer == "first_line"
    assert load_config(CONFIGS[1]).agent.embedding.provider == "ollama"
    assert load_config(CONFIGS[0]).agent.embedding.provider == "gemini"


def test_instruction_file_has_the_placeholders_and_no_other_braces():
    from pathlib import Path

    from evals import ROOT

    text = (ROOT / PROMPT).read_text(encoding="utf-8")
    for placeholder in ("{schema_summary}", "{docs_summary}", "{load_error?}"):
        assert placeholder in text
    stripped = text.replace("{schema_summary}", "").replace("{docs_summary}", "").replace("{load_error?}", "")
    assert "{" not in stripped and "}" not in stripped  # ADK would treat any other brace as a state key
    assert "Sources:" in text and "<Entity>.md" in text and "read-only" in text
    # the agent built from the config uses the file as its instruction
    assert build_agent(AgentConfig(instruction_file=Path(PROMPT))).instruction == text


def test_score_gives_table_passage_and_total_em_f1(tmp_path):
    run, ref = make_run(tmp_path)
    result = score(run, ref)
    assert result["table exact"] == 50.0 and result["table f1"] == pytest.approx(90.0)
    assert result["passage exact"] == 50.0 and result["passage f1"] == pytest.approx(50.0)
    assert result["total exact"] == 50.0 and result["total f1"] == pytest.approx(70.0)
    assert result["n"] == 4
    assert json.loads((run / "official_scores.json").read_text(encoding="utf-8")) == result


def test_score_agrees_with_our_metrics(tmp_path):
    for _, gold, pred, em, f1 in PAIRS.values():
        case = EvalCase(id="c", question="q", expected=Expected(reference_answers=[gold]))
        result = TurnResult(pred, [], None, 0.0)
        assert METRICS["exact_match"](case, result) == em
        assert METRICS["squad_f1"](case, result) == pytest.approx(f1)
    scores = official_scores(
        [{"question_id": q, "pred": v[2]} for q, v in PAIRS.items()],
        {"reference": {q: v[1] for q, v in PAIRS.items()}, "table": ["t1", "t2"], "passage": ["p1", "p2"]},
    )
    assert scores["total f1"] == pytest.approx(100 * sum(v[4] for v in PAIRS.values()) / 4)


def test_a_missing_qid_fails_cleanly(tmp_path, capsys):
    run, ref = make_run(tmp_path, drop=("p1", "p2"))
    with pytest.raises(PrepareError, match=r"2 reference question\(s\) have no prediction: p1, p2"):
        score(run, ref)
    assert not (run / "official_scores.json").exists()
    assert main(["score", str(run), "--reference", str(ref)]) == 1
    assert "no prediction" in capsys.readouterr().err


def test_missing_files_fail_cleanly(tmp_path):
    run, ref = make_run(tmp_path)
    (run / "predictions.jsonl").unlink()
    with pytest.raises(PrepareError, match="eval.predictions=true"):
        score(run, ref)


def test_cli_score_prints_the_scores(tmp_path, capsys):
    run, ref = make_run(tmp_path)
    assert main(["score", str(run), "--reference", str(ref)]) == 0
    assert json.loads(capsys.readouterr().out)["total exact"] == 50.0
