"""Tests for the semantic response cache (Sprint 9, Phase 9.1): shared cache module, lookup and store callbacks.

No network: the callbacks run on fake contexts with canned ADK events, the embedding is a fake, and one
end-to-end test drives a real ADK ``Runner`` with a scripted model.
"""

import numpy as np
import pytest
from google.adk.events import Event
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import Field
from types import SimpleNamespace

from data_agent import agent as agent_module
from data_agent import cache, doc_ingest, intent, runner, sessions
from data_agent.agent import (
    build_agent,
    make_build_data,
    make_classify,
    make_ensure_data,
    make_response_lookup,
    make_response_store,
)
from data_agent.config import AgentConfig, CacheConfig, ResponseCacheConfig, load_config
from data_agent.events import TurnResult, parse_events, turn_timing
from data_agent.runner import AgentSession
from tests.test_agent import root  # noqa: F401  (a fixture: redirects the session folders)


@pytest.fixture(autouse=True)
def _fresh_caches():
    cache.reset()
    yield
    cache.reset()


def _cfg(**response) -> AgentConfig:
    return AgentConfig(cache=CacheConfig(response=ResponseCacheConfig(enabled=True, scope="dataset", **response)))


# --- shared cache module ---


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_normalize_folds_case_width_and_whitespace():
    assert cache.normalize("  How  MANY\tClients?\n") == "how many clients?"
    assert cache.normalize("ＡＢＣ １２３") == "abc 123"  # NFKC
    assert cache.text_hash("A  b") == cache.text_hash("a B") != cache.text_hash("a c")


def test_entity_tokens_cover_numbers_ids_quotes_files_and_names():
    t = cache.entity_tokens('What is the limit for client 252 with POL-12, "Gold Plus", file rules.md, owner Maria Lopez?')
    assert {"n:252", "i:pol-12", "q:gold plus", "f:rules.md", "c:maria", "c:lopez"} <= t
    assert "c:what" not in t  # a sentence start is not a name
    assert cache.entity_tokens("Is $10,000 over the limit?") != cache.entity_tokens("Is 10000 over the limit?")
    assert cache.entity_tokens("client 252") != cache.entity_tokens("client 253")
    assert cache.entity_tokens("List the clients. Which One?") == frozenset({"c:one"})  # 'Which' starts a sentence


def test_cosine_best_picks_the_nearest_and_skips_wrong_sizes():
    best = cache.cosine_best(np.array([1.0, 0.0]), [("a", np.array([0.0, 1.0])), ("b", np.array([0.9, 0.1])), ("c", np.ones(3))])
    assert best[0] == "b" and best[1] > 0.99
    assert cache.cosine_best(np.zeros(2), [("a", np.ones(2))]) is None and cache.cosine_best(np.ones(2), []) is None


def test_store_expires_entries_and_evicts_least_recently_used():
    clock = Clock()
    store = cache.TTLStore(max_entries=2, ttl_s=10, clock=clock)
    store.put(("s", "a"), 1)
    store.put(("s", "b"), 2)
    assert store.get(("s", "a")) == 1  # a is now the most recently used
    store.put(("s", "c"), 3)
    assert store.get(("s", "b")) is None and store.get(("s", "a")) == 1 and len(store) == 2
    clock.t = 11
    assert store.get(("s", "a")) is None and store.scan(("s",)) == []
    store.put(("s", "d"), 4, ttl_s=1)
    clock.t = 13
    assert store.get(("s", "d")) is None


def test_drop_removes_one_scope_only_and_ignores_the_shared_scope():
    store = cache.get_store("x", max_entries=10, ttl_s=60)
    store.put(("s1", "k"), 1)
    store.put(("s2", "k"), 2)
    store.put(("", "k"), 3)
    assert cache.drop("s1") == 1 and cache.drop("") == 0
    assert store.get(("s1", "k")) is None and store.get(("s2", "k")) == 2 and store.get(("", "k")) == 3
    assert cache.get_store("x", max_entries=5, ttl_s=1) is store and store.max_entries == 5  # same store, new limits


def test_file_fp_follows_the_bytes_and_is_none_for_a_missing_file(tmp_path):
    f = tmp_path / "a.db"
    f.write_bytes(b"one")
    first = cache.file_fp(f)
    assert first == cache.file_fp(f) and len(first) == 64
    f.write_bytes(b"two!")
    assert cache.file_fp(f) != first
    assert cache.file_fp(tmp_path / "nope") is None and cache.file_fp(None) is None


def test_cfg_fp_ignores_cache_and_latency_trace_but_not_answer_settings():
    base = AgentConfig().model_dump(mode="json")
    fp = cache.cfg_fp(base, "instruction", None)
    assert fp == cache.cfg_fp(AgentConfig(latency_trace=True, cache=CacheConfig(response=ResponseCacheConfig(enabled=True))).model_dump(mode="json"), "instruction", None)
    assert fp != cache.cfg_fp(base, "instruction 2", None) != cache.cfg_fp(base, "instruction", "")
    assert fp != cache.cfg_fp(AgentConfig(profile="lakequest").model_dump(mode="json"), "instruction", None)


# --- configuration ---


def test_cache_is_off_by_default_and_validated():
    rc = AgentConfig().cache.response
    assert (rc.enabled, rc.semantic, rc.shadow, rc.entity_guard) == (False, False, False, True)
    assert (rc.threshold, rc.ttl_s, rc.max_entries, rc.scope) == (0.97, 3600, 1000, "session")
    with pytest.raises(ValueError):
        ResponseCacheConfig(scope="global")
    with pytest.raises(ValueError):
        CacheConfig.model_validate({"response": {"enabled": True, "bogus": 1}})


def test_dotted_overrides_reach_the_cache_block(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("name: t\ndataset:\n  path: d.yaml\n", encoding="utf-8")
    cfg = load_config(path, ["agent.cache.response.enabled=true", "agent.cache.response.threshold=0.9"])
    assert cfg.agent.cache.response.enabled and cfg.agent.cache.response.threshold == 0.9


# --- lookup and store on fake contexts ---


class World:
    """A fake session: files for the fingerprints, a user message and the events of the turn."""

    def __init__(self, tmp_path, session_id="s1", data=b"data", docs=b"docs", question="How many clients?", extra_user_turns=0):
        self.dir = tmp_path
        self.data, self.docs = tmp_path / "data.db", tmp_path / "docs.db"
        self.data.write_bytes(data)
        self.docs.write_bytes(docs)
        self.events = [SimpleNamespace(author="user", invocation_id="i0")] * (1 + extra_user_turns)
        self.ctx = SimpleNamespace(
            state={"db_path": str(self.data), "docs_db_path": str(self.docs), "docs_summary": "d", "intent": "database"},
            session=SimpleNamespace(id=session_id, events=self.events),
            user_content=types.Content(role="user", parts=[types.Part(text=question)]),
            invocation_id="i1",
        )

    def answer(self, text="42 clients.", calls=()):
        """Appends the model events of a finished turn: tool calls with responses, then the answer."""
        for i, (name, args, response) in enumerate(calls):
            self.events.append(Event(author="qa", invocation_id="i1", content=types.Content(
                role="model", parts=[types.Part(function_call=types.FunctionCall(id=f"c{i}", name=name, args=args))])))
            self.events.append(Event(author="qa", invocation_id="i1", content=types.Content(
                role="user", parts=[types.Part(function_response=types.FunctionResponse(id=f"c{i}", name=name, response=response))])))
        self.events.append(Event(author="qa", invocation_id="i1", content=types.Content(role="model", parts=[types.Part(text=text)])))


def _pair(cfg):
    fp = cache.cfg_fp(cfg.model_dump(mode="json"), "instr", None)
    return make_response_lookup(cfg, fp), make_response_store(cfg, fp)


def _turn(world, cfg, text="42 clients.", calls=()):
    """Runs lookup; on a miss also records the answer and runs store. Returns (content, outcome)."""
    lookup, store = _pair(cfg)
    content = lookup(world.ctx)
    if content is None:
        world.answer(text, calls)
        store(world.ctx)
    return content, world.ctx.state["cache"]


def test_exact_repeat_hits_and_returns_the_stored_answer(tmp_path):
    cfg = _cfg()
    content, outcome = _turn(World(tmp_path), cfg)
    assert content is None and outcome["response"] == "miss"
    hit, outcome = _turn(World(tmp_path, session_id="s2", question="  how MANY clients? "), cfg)  # normalised key, shared scope
    assert hit.parts[0].text == "42 clients." and outcome["response"] == "hit_exact" and outcome["similarity"] == 1.0
    assert outcome["entry"] and outcome["age_s"] >= 0


def test_a_hit_sets_intent_and_carries_the_stored_call_summary(tmp_path):
    cfg = _cfg()
    sql = ("run_sql", {"query": "SELECT 1"}, {"status": "success", "columns": ["a"], "rows": [[1]], "row_count": 1, "truncated": False})
    doc = ("search_documents", {"query": "limit"}, {"status": "success", "notice": "n", "passages": [
        {"chunk_id": 7, "source": "p.md", "section_path": "1 A", "section_number": "1", "page_start": None, "page_end": None,
         "text": "SECRET PASSAGE", "score": 0.4, "matched_by": "bm25"}]})
    _turn(World(tmp_path), cfg, calls=[sql, doc])
    w = World(tmp_path, session_id="s2")
    w.ctx.state["intent"] = "stale"
    hit, outcome = _turn(w, cfg)
    assert w.ctx.state["intent"] == "database" and w.ctx.state["intent_reason"] == "cache_hit"
    calls = outcome["calls"]
    assert [c["name"] for c in calls] == ["run_sql", "search_documents"] and calls[0]["args"] == {"query": "SELECT 1"}
    assert calls[0]["response"] == {"status": "success", "row_count": 1, "truncated": False}  # no rows
    assert calls[1]["response"]["passages"] == [{"chunk_id": 7, "source": "p.md", "section_path": "1 A", "section_number": "1", "page_start": None, "page_end": None}]
    assert "SECRET PASSAGE" not in str(outcome)  # no passage text anywhere


def test_a_hit_fills_calls_and_cache_in_the_turn_result():
    calls = [{"name": "run_sql", "args": {"query": "q"}, "response": {"status": "success", "row_count": 1, "truncated": False}}]
    events = [{
        "content": {"role": "model", "parts": [{"text": "cached"}]},
        "actions": {"stateDelta": {"intent": "database", "cache": {"response": "hit_exact", "similarity": 1.0, "calls": calls}}},
    }]
    result = parse_events(events, 0.2)
    assert result.answer == "cached" and result.calls == calls and result.intent == "database"
    assert result.cache == {"response": "hit_exact", "similarity": 1.0}  # the calls moved to ``calls``
    miss = parse_events([{"actions": {"stateDelta": {"cache": {"response": "miss"}}}}])
    assert miss.cache == {"response": "miss"} and miss.calls == []
    assert TurnResult("a").cache is None and parse_events([]).cache is None


def test_the_hit_answer_is_not_an_llm_stage():
    hit = {"content": {"role": "model", "parts": [{"text": "cached"}]}, "actions": {"stateDelta": {"cache": {"response": "hit_exact"}}}}
    t = turn_timing([hit], [0.05], [{"stage": "cache_lookup", "s": 0.01}], latency_s=0.06)
    assert [s["stage"] for s in t["stages"]] == ["cache_lookup"] and t["ok"]
    miss = {"content": {"role": "model", "parts": [{"text": "x"}]}}
    assert [s["stage"] for s in turn_timing([miss], [1.0], [], 1.0)["stages"]] == ["llm"]


def test_a_follow_up_in_the_same_session_bypasses(tmp_path):
    cfg = _cfg()
    _turn(World(tmp_path), cfg)
    content, outcome = _turn(World(tmp_path, extra_user_turns=1), cfg)  # same question, second user message
    assert content is None and outcome == {"response": "bypass", "reason": "follow_up"}
    w = World(tmp_path)
    w.ctx.session.events = None  # no way to tell whether this is the first turn
    assert _pair(cfg)[0](w.ctx) is None and w.ctx.state["cache"]["reason"] == "no_history"


@pytest.mark.parametrize("question,reason", [
    ("How many clients joined today?", "time_relative"),
    ("What is the latest policy?", "time_relative"),
    ("Show this month's loans", "time_relative"),
    ("", "empty"),
    ("x" * 2001, "too_long"),
])
def test_ineligible_questions_bypass_and_are_not_stored(tmp_path, question, reason):
    cfg = _cfg()
    w = World(tmp_path, question=question)
    content, outcome = _turn(w, cfg)
    assert content is None and outcome["response"] == "bypass" and outcome["reason"] == reason
    assert len(cache.get_store("response", max_entries=10, ttl_s=1)) == 0


def test_load_error_and_missing_fingerprints_bypass(tmp_path):
    cfg = _cfg()
    w = World(tmp_path)
    w.ctx.state["load_error"] = "a file could not be loaded"
    assert _turn(w, cfg)[1]["reason"] == "load_error"
    w = World(tmp_path)
    w.data.unlink()
    assert _turn(w, cfg)[1]["reason"] == "no_fingerprint"
    w = World(tmp_path)
    del w.ctx.state["docs_summary"]
    del w.ctx.state["docs_db_path"]
    assert _turn(w, cfg)[1]["reason"] == "no_fingerprint"


def test_a_session_without_documents_is_cacheable(tmp_path):
    cfg = _cfg()
    for sid, expected in (("a", "miss"), ("b", "hit_exact")):
        w = World(tmp_path, session_id=sid)
        w.ctx.state["docs_db_path"], w.ctx.state["docs_summary"] = None, "No documents loaded"
        assert _turn(w, cfg)[1]["response"] == expected


@pytest.mark.parametrize("data,docs,config", [
    (b"changed", b"docs", "same"),
    (b"data", b"changed", "same"),
    (b"data", b"docs", "other"),
])
def test_each_fingerprint_invalidates(tmp_path, data, docs, config):
    cfg = _cfg()
    _turn(World(tmp_path), cfg)
    sub = tmp_path / "other"
    sub.mkdir()
    w = World(sub, session_id="s2", data=data, docs=docs)
    use = cfg if config == "same" else AgentConfig(
        profile="lakequest", cache=CacheConfig(response=ResponseCacheConfig(enabled=True, scope="dataset")))
    lookup = make_response_lookup(use, cache.cfg_fp(use.model_dump(mode="json"), "instr", None))
    assert lookup(w.ctx) is None and w.ctx.state["cache"]["response"] == "miss"


def test_the_instruction_text_is_part_of_the_config_fingerprint():
    cfg = AgentConfig()
    assert agent_module._config_fingerprint(cfg, "a", None) != agent_module._config_fingerprint(cfg, "b", None)
    assert agent_module._config_fingerprint(cfg, "a", None) != agent_module._config_fingerprint(cfg, "a", "custom {question}")
    assert agent_module._config_fingerprint(cfg, "a", None) == agent_module._config_fingerprint(
        AgentConfig(latency_trace=True, cache=CacheConfig(response=ResponseCacheConfig(enabled=True))), "a", None)


def test_session_scope_isolates_sessions_and_drop_clears_one(tmp_path):
    cfg = AgentConfig(cache=CacheConfig(response=ResponseCacheConfig(enabled=True)))  # scope: session
    assert _turn(World(tmp_path, session_id="s1"), cfg)[1]["response"] == "miss"
    assert _turn(World(tmp_path, session_id="s2"), cfg)[1]["response"] == "miss"  # not shared
    # each session now holds its own entry, but a follow-up is bypassed, so test through a fresh lookup on the same scope
    for sid in ("s1", "s2"):
        w = World(tmp_path, session_id=sid)
        assert _pair(cfg)[0](w.ctx) is not None
    assert cache.drop("s1") == 1
    assert _pair(cfg)[0](World(tmp_path, session_id="s1").ctx) is None
    assert _pair(cfg)[0](World(tmp_path, session_id="s2").ctx) is not None


def test_closing_a_session_drops_its_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "ROOT", tmp_path)
    store = cache.get_store("response", max_entries=10, ttl_s=60)
    store.put(("gone", "k"), 1)
    store.put(("kept", "k"), 2)
    folder = tmp_path / "gone"
    folder.mkdir()
    sessions.end_session(folder)
    assert store.get(("gone", "k")) is None and store.get(("kept", "k")) == 2
    store.put(("gone", "k"), 1)
    sessions.sweep_stale(max_age=-1, root=tmp_path)  # the sweep ends stale folders the same way
    session = AgentSession.__new__(AgentSession)
    session.session_id, session.adk, session._state = "gone", SimpleNamespace(delete_session=lambda **k: None), {}
    session.close()
    assert store.get(("gone", "k")) is None


def test_entries_expire_with_the_ttl(tmp_path):
    cfg = _cfg(ttl_s=5)
    _turn(World(tmp_path), cfg)
    store = cache.get_store("response", max_entries=1000, ttl_s=5)
    clock = Clock()
    store._clock = clock
    for key in list(store._data):  # re-base the stored expiry on the fake clock
        store._data[key] = (3.0, store._data[key][1])
    clock.t = 2.0
    assert _pair(cfg)[0](World(tmp_path, session_id="s9").ctx) is not None
    clock.t = 4.0
    assert _pair(cfg)[0](World(tmp_path, session_id="s9").ctx) is None


def test_lru_keeps_the_most_recent_entries(tmp_path):
    cfg = _cfg(max_entries=2)
    for q in ("question one?", "question two?"):
        _turn(World(tmp_path, question=q), cfg)
    assert _pair(cfg)[0](World(tmp_path, question="question one?").ctx) is not None  # touches "one"
    _turn(World(tmp_path, question="question three?"), cfg)
    assert _pair(cfg)[0](World(tmp_path, question="question two?").ctx) is None  # evicted
    assert _pair(cfg)[0](World(tmp_path, question="question one?").ctx) is not None


@pytest.mark.parametrize("calls,text", [
    ([("run_sql", {"query": "x"}, {"status": "error", "error_message": "no such table"})], "Sorry."),
    ([("run_sql", {"query": "x"}, {"status": "success", "rows": [], "row_count": 100, "truncated": True})], "First rows only."),
    ([], "   "),
])
def test_unsafe_turns_are_not_stored(tmp_path, calls, text):
    cfg = _cfg()
    _turn(World(tmp_path), cfg, text=text, calls=calls)
    assert len(cache.get_store("response", max_entries=10, ttl_s=1)) == 0


def test_a_call_without_a_response_is_not_stored(tmp_path):
    cfg = _cfg()
    w = World(tmp_path)
    lookup, store = _pair(cfg)
    lookup(w.ctx)
    w.events.append(Event(author="qa", invocation_id="i1", content=types.Content(
        role="model", parts=[types.Part(function_call=types.FunctionCall(id="c", name="run_sql", args={}))])))
    w.answer("done")
    store(w.ctx)
    assert len(cache.get_store("response", max_entries=10, ttl_s=1)) == 0


def test_the_store_only_stores_an_eligible_miss(tmp_path):
    cfg = _cfg()
    w = World(tmp_path)
    w.answer()
    _, store = _pair(cfg)
    store(w.ctx)  # no lookup ran: no state["cache"]
    w.ctx.state["cache"] = {"response": "bypass"}
    store(w.ctx)
    w.ctx.state["cache"] = {"response": "hit_exact"}
    store(w.ctx)
    w.ctx.state["cache"] = {"response": "miss", "error": "RuntimeError"}
    store(w.ctx)
    assert len(cache.get_store("response", max_entries=10, ttl_s=1)) == 0


# --- semantic tier ---


class FakeEmbed:
    """Maps question text to fixed vectors; unknown text gets an orthogonal one."""

    def __init__(self, vectors):
        self.vectors, self.calls = vectors, []

    def __call__(self, texts, task_type, embedding=None):
        self.calls.append(list(texts))
        out = np.zeros((len(texts), 768), dtype=np.float32)
        for i, t in enumerate(texts):
            out[i, self.vectors.get(t, 7)] = 1.0
        return out


@pytest.fixture
def embed(monkeypatch):
    fake = FakeEmbed({"What is the limit for client 252?": 0, "Tell me the limit of client 252.": 0,
                      "What is the limit for client 253?": 0, "Unrelated question": 5})
    monkeypatch.setattr(doc_ingest, "embed", fake)
    return fake


def test_exact_only_mode_never_embeds(tmp_path, embed):
    cfg = _cfg()
    _turn(World(tmp_path, question="What is the limit for client 252?"), cfg)
    assert _turn(World(tmp_path, session_id="b", question="Tell me the limit of client 252."), cfg)[1]["response"] == "miss"
    assert embed.calls == []


def test_a_paraphrase_hits_when_similar_enough_and_the_guard_passes(tmp_path, embed):
    cfg = _cfg(semantic=True)
    assert _turn(World(tmp_path, question="What is the limit for client 252?"), cfg)[1]["response"] == "miss"
    hit, outcome = _turn(World(tmp_path, session_id="b", question="Tell me the limit of client 252."), cfg)
    assert outcome["response"] == "hit_semantic" and outcome["similarity"] == pytest.approx(1.0) and hit is not None


def test_the_entity_guard_blocks_a_near_miss_and_turning_it_off_does_not(tmp_path, embed):
    guarded = _cfg(semantic=True)
    _turn(World(tmp_path, question="What is the limit for client 252?"), guarded)
    content, outcome = _turn(World(tmp_path, session_id="b", question="What is the limit for client 253?"), guarded)
    assert content is None and outcome["response"] == "miss"


def test_without_the_guard_the_wrong_client_would_be_served(tmp_path, embed):
    cfg = _cfg(semantic=True, entity_guard=False)
    _turn(World(tmp_path, question="What is the limit for client 252?"), cfg)
    assert _turn(World(tmp_path, session_id="b", question="What is the limit for client 253?"), cfg)[1]["response"] == "hit_semantic"


def test_below_the_threshold_is_a_miss(tmp_path, embed):
    cfg = _cfg(semantic=True)
    _turn(World(tmp_path, question="What is the limit for client 252?"), cfg)
    assert _turn(World(tmp_path, session_id="b", question="Unrelated question"), cfg)[1]["response"] == "miss"


def test_shadow_reports_the_nearest_entry_but_never_serves(tmp_path, embed):
    cfg = _cfg(semantic=True, shadow=True)
    first = _turn(World(tmp_path, question="What is the limit for client 252?"), cfg)
    assert first[1] == {"response": "miss"}  # nothing to compare with yet
    content, outcome = _turn(World(tmp_path, session_id="b", question="Tell me the limit of client 252."), cfg)
    assert content is None and outcome["response"] == "miss"
    assert outcome["shadow"]["guard"] is True and outcome["shadow"]["similarity"] == pytest.approx(1.0)
    assert outcome["shadow"]["nearest"] == cache.text_hash("What is the limit for client 252?")[:12]
    _, blocked = _turn(World(tmp_path, session_id="c", question="What is the limit for client 253?"), cfg)
    assert blocked["shadow"]["guard"] is False
    exact, outcome = _turn(World(tmp_path, session_id="d", question="What is the limit for client 252?"), cfg)
    assert outcome["response"] == "hit_exact"  # shadow only concerns the semantic tier


def test_the_question_embedding_is_computed_once(tmp_path, embed):
    cfg = _cfg(semantic=True)
    _turn(World(tmp_path, question="What is the limit for client 252?"), cfg)  # lookup embeds, store reuses it
    assert embed.calls == [["What is the limit for client 252?"]]
    stored = list(cache.get_store("response", max_entries=10, ttl_s=1)._data.values())[0][1]
    assert stored.embedding is not None


def test_a_failed_embedding_falls_back_to_the_exact_tier(tmp_path, monkeypatch):
    monkeypatch.setattr(doc_ingest, "embed", lambda texts, task_type, embedding=None: None)
    cfg = _cfg(semantic=True)
    assert _turn(World(tmp_path), cfg)[1]["response"] == "miss"
    assert _turn(World(tmp_path, session_id="b"), cfg)[1]["response"] == "hit_exact"


# --- fallback and off ---


def test_a_lookup_error_is_a_miss_and_the_turn_still_stores_nothing(tmp_path, monkeypatch, caplog):
    cfg = _cfg(semantic=True)

    def boom(*a, **k):
        raise RuntimeError("secret question text")

    monkeypatch.setattr(agent_module.cache_lib, "file_fp", boom)
    w = World(tmp_path)
    assert _pair(cfg)[0](w.ctx) is None
    assert w.ctx.state["cache"] == {"response": "miss", "error": "RuntimeError"}
    w.answer()
    _pair(cfg)[1](w.ctx)
    assert len(cache.get_store("response", max_entries=10, ttl_s=1)) == 0
    assert "secret question text" not in caplog.text and "RuntimeError" in caplog.text  # type only


def test_a_store_error_is_ignored(tmp_path, monkeypatch, caplog):
    cfg = _cfg()
    lookup, store = _pair(cfg)
    w = World(tmp_path)
    lookup(w.ctx)
    w.answer()
    monkeypatch.setattr(agent_module, "_response_store", lambda cfg: (_ for _ in ()).throw(ValueError("secret")))
    assert store(w.ctx) is None
    assert "ValueError" in caplog.text and "secret" not in caplog.text


def test_an_embedding_error_is_a_miss_and_the_turn_is_not_stored(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise TimeoutError("x")

    monkeypatch.setattr(doc_ingest, "embed", boom)
    cfg = _cfg(semantic=True)
    content, outcome = _turn(World(tmp_path), cfg)
    assert content is None and outcome == {"response": "miss", "error": "TimeoutError"}
    assert len(cache.get_store("response", max_entries=10, ttl_s=1)) == 0


def test_off_is_identical_to_before():
    default = build_agent(AgentConfig())
    assert callable(default.before_agent_callback) and not isinstance(default.before_agent_callback, list)
    assert default.after_agent_callback is None
    on = build_agent(_cfg())
    assert isinstance(on.before_agent_callback, list) and len(on.before_agent_callback) == 3 and callable(on.after_agent_callback)


def test_off_writes_no_cache_state_and_keeps_old_results(root):
    ctx = SimpleNamespace(state={}, session=SimpleNamespace(id="s1"))
    make_ensure_data(AgentConfig())(ctx)
    assert "cache" not in ctx.state and ctx.state["agent_config"]["cache"]["response"]["enabled"] is False
    assert parse_events([{"content": {"role": "model", "parts": [{"text": "a"}]}}]).cache is None


def test_the_split_callbacks_do_the_work_of_ensure_data(root, monkeypatch):
    monkeypatch.setattr(intent, "classify", lambda *a, **k: intent.IntentResult(intent="document", reason="r"))
    cfg = AgentConfig(latency_trace=True, cache=CacheConfig(response=ResponseCacheConfig(enabled=True, scope="dataset")))
    ctx = SimpleNamespace(
        state={}, session=SimpleNamespace(id="s1", events=[SimpleNamespace(author="user", invocation_id="i")]),
        user_content=types.Content(role="user", parts=[types.Part(text="q")]), invocation_id="i",
    )
    fp = cache.cfg_fp(cfg.model_dump(mode="json"), "instr", None)
    make_build_data(cfg)(ctx)
    assert make_response_lookup(cfg, fp)(ctx) is None
    make_classify(cfg, None)(ctx)
    assert ctx.state["intent"] == "document" and ctx.state["cache"]["response"] == "miss"
    assert [s["stage"] for s in ctx.state["turn_timing"]] == ["data_build", "docs_build", "cache_lookup", "intent"]


# --- end to end through a real ADK Runner with a scripted model ---


class ScriptedLlm(BaseLlm):
    """A model that first calls ``run_sql`` and then answers; counts its calls."""

    model: str = "scripted"
    seen: list = Field(default_factory=list)

    async def generate_content_async(self, llm_request, stream=False):
        self.seen.append(1)
        last = llm_request.contents[-1]
        if any(p.function_response for p in last.parts or []):
            yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text="There are 60 shipments.")]))
        else:
            yield LlmResponse(content=types.Content(role="model", parts=[types.Part(
                function_call=types.FunctionCall(name="run_sql", args={"query": 'SELECT COUNT(*) FROM "data"'}))]))


def test_end_to_end_a_repeat_is_served_without_the_model(root, fake_embed, monkeypatch):
    llm = ScriptedLlm()
    classified = []

    def classify(*a, **k):
        classified.append(1)
        return intent.IntentResult(intent="database", reason="r")

    def build(cfg):
        agent = build_agent(cfg)
        agent.model = llm
        return agent

    monkeypatch.setattr(intent, "classify", classify)
    monkeypatch.setattr(runner, "build_agent", build)
    cfg = AgentConfig(latency_trace=True, cache=CacheConfig(response=ResponseCacheConfig(enabled=True, scope="dataset")))

    first = AgentSession(cfg, {}, "e1").ask("How many shipments are there?")
    assert first.error is None and first.answer == "There are 60 shipments." and first.cache == {"response": "miss"}
    assert len(llm.seen) == 2 and len(classified) == 1
    assert [s["stage"] for s in first.timing["stages"]][:3] == ["data_build", "docs_build", "cache_lookup"]
    assert "llm" in [s["stage"] for s in first.timing["stages"]]

    second = AgentSession(cfg, {}, "e2").ask("how many  SHIPMENTS are there?")
    assert second.error is None and second.answer == first.answer and second.intent == "database"
    assert second.cache["response"] == "hit_exact" and "calls" not in second.cache
    assert [c["name"] for c in second.calls] == [c["name"] for c in first.calls] == ["run_sql"]
    assert len(llm.seen) == 2 and len(classified) == 1  # no model call, no classifier call
    stages = [s["stage"] for s in second.timing["stages"]]
    assert "cache_lookup" in stages and "llm" not in stages and "intent" not in stages and "tool" not in stages
    assert second.latency_s < first.latency_s or second.latency_s < 1

    # a follow-up in the same session is never answered from the cache
    session = AgentSession(cfg, {}, "e3")
    session.ask("How many shipments are there?")  # hit
    follow = session.ask("How many shipments are there?")
    assert follow.cache["response"] == "bypass" and follow.cache["reason"] == "follow_up"
    assert len(llm.seen) == 4  # the follow-up ran the model again
