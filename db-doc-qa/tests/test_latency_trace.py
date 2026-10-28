"""Tests for the opt-in latency trace (Sprint 8, Phase 8.1): no network, canned events and fake sessions."""

import json
from types import SimpleNamespace

import pytest
import yaml
from google.adk.events import Event
from google.adk.events.event_actions import EventActions
from google.genai import types

import evals.run as eval_run
from data_agent import agent as agent_module
from data_agent import intent, runner
from data_agent.agent import make_ensure_data
from data_agent.config import AgentConfig, load_config
from data_agent.events import TurnResult, parse_events, summarize_stages, turn_timing
from data_agent.runner import AgentSession
from evals.__main__ import format_summary
from evals.run import run_eval
from tests.test_agent import _ctx, root  # noqa: F401  (root is a fixture)
from tests.test_eval_run import CONFIG, FakeSession


# --- turn_timing: pure derivation from canned events ---


def _model(*parts, usage=None):
    ev = {"content": {"role": "model", "parts": list(parts)}}
    if usage:
        ev["usageMetadata"] = usage
    return ev


def _fc(name):
    return {"functionCall": {"name": name, "args": {}}}


def _fr(name):
    return {"functionResponse": {"name": name, "response": {}}}


def _stages(timing):
    return [(s["stage"], s["s"]) for s in timing["stages"]]


def _names(timing):
    return [s["stage"] for s in timing["stages"]]


def test_turn_result_timing_defaults_to_none():
    assert TurnResult("a").timing is None and parse_events([]).timing is None


def test_sequential_turn_splits_into_stages_that_add_up_to_the_latency():
    events = [
        {"actions": {"stateDelta": {"turn_timing": []}}},  # no content: skipped
        _model(_fc("run_sql"), usage={"promptTokenCount": 100, "candidatesTokenCount": 5}),
        _model(_fr("run_sql")),
        _model(_fc("search_documents")),
        _model(_fr("search_documents")),
        _model({"text": "done"}, usage={"promptTokenCount": 300, "candidatesTokenCount": 40}),
    ]
    arrivals = [0.5, 1.5, 1.7, 2.4, 2.9, 4.0]
    t = turn_timing(events, arrivals, [{"stage": "intent", "s": 0.5}], latency_s=4.1)
    assert _names(t) == ["intent", "llm", "tool", "llm", "tool", "llm"]
    # the intent call ran inside the wait for the first event, so it is taken off the first llm stage
    assert [s for _, s in _stages(t)] == pytest.approx([0.5, 1.0, 0.2, 0.7, 0.5, 1.1])
    assert t["other_s"] == pytest.approx(0.1) and t["ok"] is True
    assert sum(s for _, s in _stages(t)) + t["other_s"] == pytest.approx(4.1)  # no double counting
    assert t["stages"][1]["in_tok"] == 100 and t["stages"][1]["out_tok"] == 5
    assert t["stages"][2]["names"] == ["run_sql"] and "in_tok" not in t["stages"][2]


def test_parallel_tool_calls_make_one_tool_stage():
    events = [
        _model(_fc("run_sql"), _fc("search_documents")),
        _model(_fr("run_sql"), _fr("search_documents")),
        _model({"text": "x"}),
    ]
    t = turn_timing(events, [1.0, 2.0, 3.0], [], latency_s=3.0)
    assert _names(t) == ["llm", "tool", "llm"]
    assert t["stages"][1]["names"] == ["run_sql", "search_documents"]


def test_a_turn_without_tool_calls_has_no_tool_stage():
    t = turn_timing([_model({"text": "No, read-only."})], [0.8], [], latency_s=0.8)
    assert _names(t) == ["llm"] and t["other_s"] == pytest.approx(0.0)


def test_only_the_setup_that_ran_is_listed_and_query_s_leaves_out_ingestion():
    setup = [{"stage": "data_build", "s": 0.1}, {"stage": "docs_build", "s": 2.0}]
    t = turn_timing([_model({"text": "x"})], [3.0], setup, latency_s=3.0)
    assert _names(t) == ["data_build", "docs_build", "llm"]
    assert t["query_s"] == pytest.approx(0.9)  # latency 3.0 minus 2.1 of ingestion
    # a prebuilt bundle: nothing in the setup, so there are no build stages and query_s is the whole turn
    t = turn_timing([_model({"text": "x"})], [0.9], [], latency_s=0.9)
    assert _names(t) == ["llm"] and t["query_s"] == pytest.approx(0.9)


def test_an_error_mid_turn_keeps_the_stages_so_far_and_puts_the_rest_in_other():
    t = turn_timing([_model(_fc("run_sql"))], [1.0], [], latency_s=2.5)  # the run died after the first call
    assert _names(t) == ["llm"] and t["other_s"] == pytest.approx(1.5) and t["ok"] is True
    assert turn_timing([], [], [], latency_s=0.3)["stages"] == []  # nothing arrived at all


def test_overlapping_stages_are_marked_not_ok():
    t = turn_timing([_model({"text": "x"})], [1.0], [{"stage": "intent", "s": 3.0}], latency_s=1.0)
    assert t["other_s"] < 0 and t["ok"] is False
    assert summarize_stages([{"stage": "llm", "s": 1.005}], 1.0)["ok"] is True  # within the 10 ms tolerance


def test_partial_chunks_are_skipped():
    events = [{**_model({"text": "he"}), "partial": True}, _model({"text": "hello"})]
    assert _names(turn_timing(events, [0.4, 1.0], [], latency_s=1.0)) == ["llm"]


# --- ensure_data: the setup steps that ran ---


def _setup_stages(ctx, monkeypatch, **cfg):
    monkeypatch.setattr(intent, "classify", lambda *a, **k: intent.IntentResult(intent="database", reason="r"))
    ctx.user_content = SimpleNamespace(parts=[SimpleNamespace(text="q")])
    make_ensure_data(AgentConfig(**cfg))(ctx)
    return [e["stage"] for e in ctx.state.get("turn_timing", [])]


def test_flag_off_writes_no_turn_timing(root, monkeypatch):
    ctx = _ctx()
    assert _setup_stages(ctx, monkeypatch) == [] and "turn_timing" not in ctx.state


def test_flag_on_lists_only_the_steps_that_ran(root, monkeypatch):
    ctx = _ctx()
    assert _setup_stages(ctx, monkeypatch, latency_trace=True) == ["data_build", "docs_build", "intent"]
    assert all(e["s"] >= 0 for e in ctx.state["turn_timing"])
    # second turn: both databases exist, so only the intent step is left; the value is replaced, not appended
    assert _setup_stages(ctx, monkeypatch, latency_trace=True) == ["intent"]
    # intent off: nothing ran, so the list is empty
    assert _setup_stages(ctx, monkeypatch, latency_trace=True, intent_enabled=False) == []


def test_flag_on_has_no_docs_build_without_documents(root, monkeypatch):
    ctx = _ctx(docs=[])
    assert _setup_stages(ctx, monkeypatch, latency_trace=True, intent_enabled=False) == ["data_build"]


# --- AgentSession ---


def _event(*parts, delta=None):
    content = types.Content(role="model", parts=list(parts)) if parts else None
    return Event(author="qa_orchestrator", content=content, actions=EventActions(state_delta=delta or {}))


class FakeRunner:
    def __init__(self, events=(), exc=None):
        self.events, self.exc = list(events), exc

    async def run_async(self, user_id, session_id, new_message):
        for ev in self.events:
            yield ev
        if self.exc:
            raise self.exc


@pytest.fixture
def patched(monkeypatch):
    box = type("Box", (), {"events": [], "exc": None})()
    monkeypatch.setattr(runner, "Runner", lambda **kw: FakeRunner(box.events, box.exc))
    return box


def _call_event():
    return _event(types.Part(function_call=types.FunctionCall(id="1", name="run_sql", args={})))


def _traced_events():
    return [
        _event(delta={"intent": "database", "turn_timing": [{"stage": "intent", "s": 0.0}]}),
        _call_event(),
        _event(types.Part(function_response=types.FunctionResponse(id="1", name="run_sql", response={"status": "success"}))),
        _event(types.Part(text="Final answer.")),
    ]


def test_session_flag_off_leaves_timing_empty(patched):
    patched.events = _traced_events()
    assert AgentSession(AgentConfig(), {}).ask("q").timing is None


def test_session_flag_on_builds_the_stages_from_the_events(patched):
    patched.events = _traced_events()
    result = AgentSession(AgentConfig(latency_trace=True), {}).ask("q")
    assert _names(result.timing) == ["intent", "llm", "tool", "llm"]
    assert result.timing["ok"] is True and result.answer == "Final answer."


def test_session_reads_the_setup_from_the_session_state_when_no_event_has_it(patched):
    patched.events = [_event(delta={"intent": "database"}), _event(types.Part(text="Hi"))]
    state = {"turn_timing": [{"stage": "docs_build", "s": 0.0}]}
    result = AgentSession(AgentConfig(latency_trace=True), state).ask("q")
    assert _names(result.timing) == ["docs_build", "llm"]


def test_session_keeps_the_partial_trace_after_an_error(patched):
    patched.events = [_call_event()]
    patched.exc = RuntimeError("boom")
    result = AgentSession(AgentConfig(latency_trace=True), {}).ask("q")
    assert result.error == "RuntimeError: boom" and _names(result.timing) == ["llm"]


# --- run_eval: rows, aggregates and the summary ---


class TracedSession(FakeSession):
    """Canned results with a trace: setup 0.5 s (data 0.1 + docs 0.4), llm 1.0 + 0.5, tool 0.5, 0.1 left over."""

    def ask(self, question):
        stages = [
            {"stage": "data_build", "s": 0.1},
            {"stage": "docs_build", "s": 0.4},
            {"stage": "llm", "s": 1.0, "in_tok": 100, "out_tok": 10},
            {"stage": "tool", "s": 0.5, "names": ["run_sql"]},
            {"stage": "llm", "s": 0.5, "in_tok": 200, "out_tok": 30},
        ]
        return TurnResult("a", [], "database", 2.6, None, summarize_stages(stages, 2.6))


@pytest.fixture
def cfg_file(tmp_path):
    path = tmp_path / "run.yaml"
    path.write_text(yaml.safe_dump(CONFIG), encoding="utf-8")
    return path


def test_traced_rows_produce_stage_and_tag_latency(cfg_file, tmp_path):
    cases = "dataset.case_ids=[conflict_international, no_policy_refrigerated, db_location]"
    cfg = load_config(cfg_file, [cases, "agent.latency_trace=true"])
    run_dir = tmp_path / "run"
    metrics = run_eval(cfg, run_dir, session_factory=TracedSession)
    rows = [json.loads(line) for line in (run_dir / "results.jsonl").read_text().splitlines()]
    assert all(r["timing"]["ok"] for r in rows) and rows[0]["timing"]["query_s"] == pytest.approx(2.1)
    stages = metrics["latency_stages"]
    assert stages["n"] == 3 and stages["query_p50_s"] == pytest.approx(2.1)
    assert stages["in_tok_mean"] == 300 and stages["out_tok_mean"] == 40
    llm = stages["stages"]["llm"]  # the two llm entries of a turn are summed for that turn
    assert llm["turns"] == 3 and llm["calls_per_turn"] == 2 and llm["p50_s"] == pytest.approx(1.5)
    assert stages["stages"]["other"]["p50_s"] == pytest.approx(0.1)
    assert "intent" not in stages["stages"]  # a stage that never ran is absent, not zero
    assert metrics["by_tag"]["conflict"]["latency_p50_s"] == pytest.approx(2.6)
    assert json.loads((run_dir / "metrics.json").read_text()) == metrics
    text = format_summary(metrics)
    assert "stage p50" in text and "llm 1.50s" in text and "[gap]" in text and "latency p50 2.6s" in text


def test_untraced_rows_keep_the_old_keys(cfg_file, tmp_path):
    run_dir = tmp_path / "run"
    metrics = run_eval(load_config(cfg_file, ["dataset.limit=2"]), run_dir, session_factory=FakeSession)
    rows = [json.loads(line) for line in (run_dir / "results.jsonl").read_text().splitlines()]
    assert all("timing" not in r for r in rows) and "latency_stages" not in metrics
    assert all(sorted(p) == ["metrics", "n"] for p in metrics["by_tag"].values())
    assert "stage p50" not in format_summary(metrics)


def test_overlapping_traces_are_left_out_of_the_stage_percentiles(cfg_file, tmp_path):
    class Mixed(FakeSession):
        def ask(self, question):
            if question.startswith("Where"):  # more stage time than the turn: ok is false
                return TurnResult("a", [], None, 1.0, None, summarize_stages([{"stage": "llm", "s": 5.0}], 1.0))
            return TurnResult("a", [], None, 1.2, None, summarize_stages([{"stage": "llm", "s": 1.0}], 1.2))

    metrics = run_eval(load_config(cfg_file, ["dataset.limit=2", "agent.latency_trace=true"]), tmp_path / "a", session_factory=Mixed)
    assert metrics["latency_stages"]["n"] == 1 and metrics["latency_stages"]["stages"]["llm"]["p50_s"] == 1.0
    only_bad = run_eval(load_config(cfg_file, ["dataset.limit=1", "agent.latency_trace=true"]), tmp_path / "b", session_factory=Mixed)
    assert "latency_stages" not in only_bad  # no usable trace: the key is absent


def test_retrieval_mode_traces_only_the_steps_that_ran(tmp_path, fake_embed):
    from tests.test_eval_retrieval import config, dataset

    ds = dataset(tmp_path, "3.2")

    def first_timing(name, *overrides):
        run_eval(config(tmp_path, ds, *overrides), tmp_path / name)
        return json.loads((tmp_path / name / "results.jsonl").read_text().splitlines()[0]).get("timing")

    bm25 = first_timing("bm25", "agent.latency_trace=true")
    assert _names(bm25) == ["search"] and bm25["ok"] is True  # bm25 embeds no query
    hybrid = first_timing("hyb", "agent.latency_trace=true", "agent.retrieval.mode=hybrid")
    assert _names(hybrid) == ["query_embedding", "search"]
    assert first_timing("off") is None


# --- the trace uses the high-resolution clock (perf_counter), not monotonic (15.6 ms tick on Windows) ---


def _clock(*values):
    """A stand-in for the ``time`` module of one file: ``perf_counter()`` returns the given values in order."""
    it = iter(values)
    return SimpleNamespace(perf_counter=lambda: next(it))


def test_the_session_times_turns_and_events_with_perf_counter(patched, monkeypatch):
    patched.events = [_call_event(), _event(types.Part(text="done"))]
    # started, the arrival of each of the two events, then the end of the turn
    monkeypatch.setattr(runner, "time", _clock(0.0, 0.25, 0.5, 1.0))
    result = AgentSession(AgentConfig(latency_trace=True), {}).ask("q")
    assert result.error is None and result.latency_s == 1.0
    assert [s["s"] for s in result.timing["stages"]] == [0.25, 0.25] and result.timing["other_s"] == 0.5


def test_the_setup_steps_are_timed_with_perf_counter(root, monkeypatch):
    # two readings per step: data build, document build, intent
    monkeypatch.setattr(agent_module, "time", _clock(0.0, 0.5, 1.0, 3.0, 3.0, 3.25))
    monkeypatch.setattr(intent, "classify", lambda *a, **k: intent.IntentResult(intent="database", reason="r"))
    ctx = _ctx()
    ctx.user_content = SimpleNamespace(parts=[SimpleNamespace(text="q")])
    make_ensure_data(AgentConfig(latency_trace=True))(ctx)
    assert ctx.state["turn_timing"] == [
        {"stage": "data_build", "s": 0.5}, {"stage": "docs_build", "s": 2.0}, {"stage": "intent", "s": 0.25},
    ]


def test_retrieval_mode_is_timed_with_perf_counter(tmp_path, fake_embed, monkeypatch):
    from tests.test_eval_retrieval import config, dataset

    monkeypatch.setattr(eval_run, "time", _clock(0.0, 0.1, 0.35, 0.5))  # started, search start, search end, end
    run_eval(config(tmp_path, dataset(tmp_path, "3.2"), "agent.latency_trace=true"), tmp_path / "run")
    timing = json.loads((tmp_path / "run" / "results.jsonl").read_text().splitlines()[0])["timing"]
    assert [(s["stage"], s["s"]) for s in timing["stages"]] == [("search", 0.25)]
    assert timing["other_s"] == 0.25 and timing["query_s"] == 0.5
