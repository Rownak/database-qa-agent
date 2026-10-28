"""Tests for the configurable intent prompt (Sprint 8, Phase 8.3): no network, the model call is mocked."""

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml
from google import genai

from data_agent import intent
from data_agent.agent import INSTRUCTION, build_agent, ensure_data, make_ensure_data
from data_agent.config import AgentConfig, load_config
from data_agent.intent import DEFAULT_PROMPT, FALLBACK_REASON, IntentResult, check_prompt, classify
from evals import report
from evals.run import run_eval
from tests.test_agent import _ctx, root  # noqa: F401  (root is a fixture)
from tests.test_eval_run import CONFIG, FakeSession

ROOT = Path(__file__).resolve().parent.parent
LAKE_PROMPT_PATH = Path("configs/prompts/intent_lakequest.md")  # relative to db-doc-qa/, as in the YAML

# sha256 of the shipments prompt and instruction as they were committed before Phase 8.3
DEFAULT_PROMPT_SHA256 = "b9824030b8b2d7c46d43cc968328456c8197f375704cabc62254e697a4a0685f"
INSTRUCTION_SHA256 = "32ac35d0168f02893145107a7979212a3e8a7f999a522bc894c2ed8da16cd378"


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture
def gen(monkeypatch):
    """Replaces the genai client; returns the ``generate_content`` mock."""
    mock = MagicMock()
    monkeypatch.setattr(genai, "Client", lambda *a, **k: SimpleNamespace(models=SimpleNamespace(generate_content=mock)))
    return mock


def _reply(label="database"):
    return SimpleNamespace(text=json.dumps({"intent": label, "reason": "r"}))


# --- intent.py: the prompt is an argument ---


def test_the_default_prompt_and_the_shipments_instruction_are_unchanged():
    assert _sha(DEFAULT_PROMPT) == DEFAULT_PROMPT_SHA256 and intent.PROMPT is DEFAULT_PROMPT
    assert _sha(INSTRUCTION) == INSTRUCTION_SHA256


def test_classify_uses_the_given_prompt_and_the_default_otherwise(gen):
    gen.return_value = _reply()
    classify("q?", "SCHEMA", "DOCS", prompt="Bank prompt | {schema_summary} | {docs_summary} | {question}")
    assert gen.call_args.kwargs["contents"] == "Bank prompt | SCHEMA | DOCS | q?"
    classify("q?", "SCHEMA", "DOCS")
    assert gen.call_args.kwargs["contents"].startswith("Classify the user's question for a shipment assistant")


def test_a_custom_prompt_keeps_the_output_schema_and_the_fallback(gen):
    gen.return_value = _reply("document")
    assert classify("q?", prompt="{question}") == IntentResult(intent="document", reason="r")
    assert gen.call_args.kwargs["config"].response_schema is IntentResult  # the labels do not depend on the prompt
    gen.side_effect = RuntimeError("down")
    assert classify("q?", prompt="{question}") == IntentResult(intent="hybrid", reason=FALLBACK_REASON)


@pytest.mark.parametrize(
    "text, message",
    [
        ("no placeholder at all", "{question}"),
        ("{schema_summary} only", "{question}"),
        ("{unknown} and {question}", "may only use"),
        ("a stray { brace and {question}", "may only use"),
        ("{question} and a stray }", "may only use"),
    ],
)
def test_check_prompt_rejects_what_classify_could_not_format(text, message):
    with pytest.raises(ValueError, match=message.replace("{", r"\{").replace("}", r"\}")):
        check_prompt(text)


def test_check_prompt_accepts_the_default_and_returns_the_text():
    assert check_prompt(DEFAULT_PROMPT) is DEFAULT_PROMPT
    assert check_prompt("{question}") == "{question}"


def test_the_lakequest_prompt_file_is_a_valid_bank_prompt():
    text = check_prompt((ROOT / LAKE_PROMPT_PATH).read_text(encoding="utf-8"))
    for needle in ("{schema_summary}", "{docs_summary}", "{question}", "data to classify", "bank"):
        assert needle in text
    for label in ("- database:", "- document:", "- hybrid:"):  # the labels IntentResult accepts
        assert label in text
    assert "shipment" not in text.lower()


# --- agent.py: the prompt is read once, at build time ---


def _spy(monkeypatch):
    calls = []

    def fake_classify(*args, **kwargs):
        calls.append((args, kwargs))
        return IntentResult(intent="database", reason="r")

    monkeypatch.setattr(intent, "classify", fake_classify)
    return calls


def _turn(callback):
    ctx = _ctx()
    ctx.user_content = SimpleNamespace(parts=[SimpleNamespace(text="the question")])
    callback(ctx)
    return ctx


def test_the_prompt_file_is_read_once_and_passed_to_classify(root, monkeypatch, tmp_path):
    calls = _spy(monkeypatch)
    path = tmp_path / "p.md"
    path.write_text("Custom {schema_summary} {docs_summary} {question}", encoding="utf-8")
    callback = make_ensure_data(AgentConfig(intent_prompt_file=path))
    path.unlink()  # a second read would fail: the text was taken when the agent was built
    _turn(callback)
    _turn(callback)
    assert len(calls) == 2
    for args, kwargs in calls:
        assert kwargs["prompt"] == "Custom {schema_summary} {docs_summary} {question}"
        # the classifier sees the question and the two summaries only, never a case tag or an expected value
        assert len(args) == 3 and args[0] == "the question" and set(kwargs) == {"model", "prompt"}


def test_a_relative_prompt_file_is_taken_from_db_doc_qa(root, monkeypatch):
    calls = _spy(monkeypatch)
    _turn(make_ensure_data(AgentConfig(intent_prompt_file=LAKE_PROMPT_PATH)))
    assert calls[0][1]["prompt"] == (ROOT / LAKE_PROMPT_PATH).read_text(encoding="utf-8")


def test_without_a_prompt_file_classify_is_called_as_before(root, monkeypatch):
    calls = _spy(monkeypatch)
    _turn(make_ensure_data(AgentConfig()))
    assert set(calls[0][1]) == {"model"}


def test_a_missing_or_invalid_prompt_file_fails_when_the_agent_is_built(tmp_path):
    with pytest.raises(FileNotFoundError, match="intent_prompt_file"):
        make_ensure_data(AgentConfig(intent_prompt_file=tmp_path / "nope.md"))
    with pytest.raises(FileNotFoundError, match="intent_prompt_file"):
        build_agent(AgentConfig(intent_prompt_file=tmp_path / "nope.md"))
    for bad in ("no question here", "{question} {oops}", "{question} {"):
        path = tmp_path / "bad.md"
        path.write_text(bad, encoding="utf-8")
        with pytest.raises(ValueError, match="intent_prompt_file"):
            build_agent(AgentConfig(intent_prompt_file=path))


def test_with_the_classifier_off_the_prompt_file_is_not_read(root, monkeypatch, tmp_path):
    monkeypatch.setattr(intent, "classify", lambda *a, **k: pytest.fail("classify must not run"))
    callback = make_ensure_data(AgentConfig(intent_enabled=False, intent_prompt_file=tmp_path / "nope.md"))
    ctx = _turn(callback)
    assert ctx.state["intent"] == "hybrid" and ctx.state["intent_reason"] == "intent_disabled"


def test_the_lakequest_instruction_shows_the_classified_intent(root):
    from google.adk.agents.readonly_context import ReadonlyContext
    from google.adk.utils.instructions_utils import inject_session_state

    lake = build_agent(AgentConfig(profile="lakequest")).instruction
    ctx = _ctx()
    ensure_data(ctx)
    for label in ("database", "document", "hybrid"):
        ctx.state["intent"] = label
        ro = ReadonlyContext(SimpleNamespace(session=SimpleNamespace(state=ctx.state)))
        text = asyncio.run(inject_session_state(lake, ro))
        assert f"(a hint, not a rule; may be empty): {label}" in text
        assert "{" not in text and "}" not in text  # every placeholder resolved
    for rule in ('"database" means records: call only run_sql', '"document" means policy: call only search_documents', "follow rules 3 to 5"):
        assert rule in lake


# --- config ---


def test_intent_prompt_file_defaults_to_none_and_is_saved_with_forward_slashes(tmp_path):
    assert AgentConfig().intent_prompt_file is None and AgentConfig().model_dump(mode="json")["intent_prompt_file"] is None
    cfg = AgentConfig(intent_prompt_file=Path("configs") / "prompts" / "x.md")
    assert cfg.model_dump(mode="json")["intent_prompt_file"] == "configs/prompts/x.md"


def test_lakequest_bank_config_turns_the_classifier_on_with_the_bank_prompt(tmp_path):
    cfg = load_config(ROOT / "configs/eval/lakequest_bank.yaml")
    assert cfg.agent.intent_enabled is True and cfg.agent.profile == "lakequest"
    assert cfg.agent.intent_prompt_file.as_posix() == LAKE_PROMPT_PATH.as_posix()
    assert (ROOT / cfg.agent.intent_prompt_file).is_file()
    assert load_config(ROOT / "configs/eval/lakequest_bank.yaml", ["agent.intent_enabled=false"]).agent.intent_enabled is False
    assert load_config(ROOT / "configs/eval/smoke.yaml").agent.intent_prompt_file is None  # shipments keeps the default
    report.write_config(tmp_path, cfg)  # the saved config.yaml reloads to the same settings
    assert load_config(tmp_path / "config.yaml") == cfg


# --- benchmark labels are for evaluation only ---


def test_the_session_state_and_agent_config_carry_no_benchmark_labels(tmp_path):
    path = tmp_path / "run.yaml"
    path.write_text(yaml.safe_dump(CONFIG), encoding="utf-8")
    FakeSession.created = []
    run_eval(load_config(path), tmp_path / "run", session_factory=FakeSession)
    labels = ("conflict", "gap", "safety", "tool_sequence", "expected", "answer_contains", "sections")
    assert FakeSession.created
    for agent_cfg, state in FakeSession.created:
        assert set(state) <= {"uploads", "docs"}  # data files only
        assert not any(label in json.dumps(state) + json.dumps(agent_cfg.model_dump(mode="json")) for label in labels)
