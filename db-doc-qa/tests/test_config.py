"""Tests for data_agent.config and the settings threaded through ingestion and retrieval."""

import base64

import numpy as np
import pytest
import yaml
from pydantic import ValidationError

from data_agent import agent, config, doc_ingest, retrieval, tools
from data_agent.config import (
    AgentConfig,
    ChunkingConfig,
    EmbeddingConfig,
    RetrievalConfig,
    load_agent_config,
    load_config,
)
from data_agent.ingest import SAMPLE_DOCS_DIR

SMOKE = {"name": "t", "dataset": {"path": "d.yaml"}, "agent": {"retrieval": {"min_dense_sim": 0.7}}}


@pytest.fixture
def yaml_file(tmp_path):
    def write(data, name="run.yaml"):
        path = tmp_path / name
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        return path

    return write


def test_defaults_equal_the_old_constants():
    assert ChunkingConfig().chunk_words == doc_ingest.CHUNK_WORDS == 160
    assert ChunkingConfig().fallback_overlap == doc_ingest.FALLBACK_OVERLAP == 0.15
    assert EmbeddingConfig().dim == doc_ingest.EMBED_DIM == 768
    assert EmbeddingConfig().model == doc_ingest.EMBED_MODEL == "gemini-embedding-001"
    assert EmbeddingConfig().batch == doc_ingest.EMBED_BATCH == 100
    assert RetrievalConfig().rrf_k == retrieval.RRF_K == 60
    assert RetrievalConfig().top_n == retrieval.TOP_N == 20
    assert RetrievalConfig().min_dense_sim == retrieval.MIN_DENSE_SIM == 0.62
    assert RetrievalConfig().max_top_k == tools.MAX_TOP_K == 8
    assert RetrievalConfig().max_passage_chars == tools.MAX_PASSAGE_CHARS == 1200
    assert AgentConfig().model.agent == agent.AGENT_MODEL == "gemini-3.5-flash-lite"
    assert AgentConfig().model.intent is None and AgentConfig().model.temperature is None


def test_unknown_key_is_rejected(yaml_file):
    bad = {**SMOKE, "agent": {"retrieval": {"min_dense_simm": 0.5}}}
    with pytest.raises(ValidationError):
        load_config(yaml_file(bad))


def test_out_of_range_value_is_rejected(yaml_file):
    bad = {**SMOKE, "agent": {"chunking": {"chunk_words": 5}}}
    with pytest.raises(ValidationError):
        load_config(yaml_file(bad))


def test_models_are_frozen():
    with pytest.raises(ValidationError):
        AgentConfig().intent_enabled = False


def test_override_is_typed_and_beats_yaml(yaml_file):
    cfg = load_config(
        yaml_file(SMOKE),
        ["agent.retrieval.min_dense_sim=0.55", "agent.intent_enabled=false", "agent.model.intent=null", "name=other"],
    )
    assert cfg.agent.retrieval.min_dense_sim == 0.55 and isinstance(cfg.agent.retrieval.min_dense_sim, float)
    assert cfg.agent.intent_enabled is False
    assert cfg.agent.model.intent is None
    assert cfg.name == "other"


def test_yaml_beats_defaults(yaml_file):
    cfg = load_config(yaml_file(SMOKE))
    assert cfg.agent.retrieval.min_dense_sim == 0.7
    assert cfg.agent.retrieval.rrf_k == 60  # untouched keys keep the default


def test_malformed_override_is_rejected(yaml_file):
    with pytest.raises(ValueError):
        load_config(yaml_file(SMOKE), ["no_equals_sign"])


def test_resolved_config_round_trips(yaml_file):
    cfg = load_config(yaml_file(SMOKE), ["eval.fail_under.intent_accuracy=0.8"])
    again = load_config(yaml_file(cfg.model_dump(mode="json"), "again.yaml"))
    assert again == cfg


def test_load_agent_config_defaults_when_env_unset(monkeypatch):
    monkeypatch.delenv(config.CONFIG_ENV, raising=False)
    assert load_agent_config() == AgentConfig()


def test_load_agent_config_reads_the_agent_block(monkeypatch, yaml_file):
    monkeypatch.setenv(config.CONFIG_ENV, str(yaml_file(SMOKE)))
    assert load_agent_config().retrieval.min_dense_sim == 0.7


def test_chunking_config_changes_chunk_size():
    data = (SAMPLE_DOCS_DIR / "international_shipping_procedures.txt").read_bytes()
    default = doc_ingest.prepare("a.txt", data)
    small = doc_ingest.prepare("a.txt", data, ChunkingConfig(chunk_words=30))
    assert len(small.chunks) > len(default.chunks)
    assert all(len(c.text.split()) <= 30 for c in small.chunks)


def test_embedding_config_reaches_the_api(monkeypatch):
    seen = []

    class _Models:
        def embed_content(self, model, contents, config):
            seen.append((model, len(contents), config.output_dimensionality))
            return type("R", (), {"embeddings": [type("E", (), {"values": [1.0, 0.0, 0.0, 0.0]})() for _ in contents]})()

    class _Client:
        models = _Models()

    import google.genai

    monkeypatch.setattr(google.genai, "Client", lambda: _Client())
    out = doc_ingest.embed(["a", "b", "c"], "RETRIEVAL_QUERY", EmbeddingConfig(model="m", dim=4, batch=2))
    assert out.shape == (3, 4)
    assert seen == [("m", 2, 4), ("m", 1, 4)]


def test_bm25_mode_returns_only_bm25_hits(tmp_path, fake_embed):
    docs = [
        {"name": p.name, "b64": base64.b64encode(p.read_bytes()).decode()} for p in sorted(SAMPLE_DOCS_DIR.iterdir())
    ]
    db = doc_ingest.build(docs, tmp_path / "s").docs_db_path
    qvec = np.zeros(doc_ingest.EMBED_DIM, dtype=np.float32)
    qvec[0] = 1.0  # the same vector fake_embed gave every chunk, so dense search matches everything

    hybrid = retrieval.search(db, "express shipments escalation", top_k=5, query_vec=qvec)
    assert hybrid.dense_used and any(h.matched_by != "bm25" for h in hybrid.hits)

    bm25 = retrieval.search(db, "express shipments escalation", top_k=5, query_vec=qvec, cfg=RetrievalConfig(mode="bm25"))
    assert bm25.hits and {h.matched_by for h in bm25.hits} == {"bm25"} and not bm25.dense_used

    dense = retrieval.search(db, "express shipments escalation", top_k=5, query_vec=qvec, cfg=RetrievalConfig(mode="dense"))
    assert dense.hits and {h.matched_by for h in dense.hits} == {"dense"}


def test_embedding_provider_defaults_to_gemini_and_accepts_only_known_values():
    assert EmbeddingConfig().provider == "gemini" and EmbeddingConfig().base_url == "http://localhost:11434"
    assert EmbeddingConfig(provider="ollama", model="nomic-embed-text:latest").provider == "ollama"
    with pytest.raises(ValidationError):
        EmbeddingConfig(provider="openai")
