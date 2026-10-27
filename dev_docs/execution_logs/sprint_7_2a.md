# Sprint 7 — Phase 7.2a Execution Log: Selectable Embedding Provider (Gemini or Ollama)

## Summary
The embedding provider is now a setting of the run config: `agent.embedding.provider: gemini | ollama`. Gemini stays the default, so the app, the deployed agent and existing configs behave as before. With `ollama`, `nomic-embed-text:latest` (768 dimensions) runs on the local server, which avoids the Gemini embedding rate limit seen in Phase 7.2. `uv run pytest`: **376 passed** (7 new).

## What changed
| File | Change |
|---|---|
| `data_agent/config.py` | `EmbeddingConfig.provider` (`gemini` default) and `base_url` (`http://localhost:11434`, Ollama only). Still pydantic-only. |
| `data_agent/doc_ingest.py` | `embed` dispatches to `_embed_gemini` (the old code, moved unchanged) or `_embed_ollama` (`POST {base_url}/api/embed`, standard library, batches of `embedding.batch`). Same L2 normalisation and the same `None`-on-failure rule; a vector size different from `dim` raises inside the existing `try`, so a wrong-size index is never written. Documents, queries (`search_documents`) and retrieval mode all go through `embed`, so they follow the choice. |
| `evals/run.py` (`_load_prebuilt`) | The stale-bundle guard fills missing keys with defaults before comparing, and ignores `base_url`. Bundles built before this phase (no `provider` key) still match a Gemini config; a bundle built with another provider or model is rejected. |
| `configs/eval/hybridqa_dev50_ollama.yaml` | New: same as `hybridqa_dev50.yaml` plus `embedding: {provider: ollama, model: nomic-embed-text:latest, dim: 768}`, dataset `dev_s50_ollama`. |
| `README.md`, `dev_docs/design/design.md` | Rate-limit note now describes the implemented option; the design's `EmbeddingConfig` snippet shows the new fields. |

## Decisions
- **`nomic-embed-text` task prefixes.** The model is trained with `search_document: ` / `search_query: `, so `_embed_ollama` adds them for models whose name starts with `nomic-embed-text` (other Ollama models get the text unchanged). Without them retrieval quality drops.
- **`prepare` needed no change.** It takes the provider from `--config`; its retry-then-fail rule covers a stopped Ollama server (a failed embedding is a clear `PrepareError`, never a BM25-only bundle).
- **`min_dense_sim` left at 0.62.** On the dev-script pair, a relevant sentence scored 0.91 and an unrelated one 0.34, so the default threshold separates them. It was tuned for Gemini, so revisit it if Ollama runs show missing dense hits.
- **Results are not comparable across providers.** Keep one provider per result series; the bundle folders are separate (`dev_s50` Gemini, `dev_s50_ollama`).

## Real bundle
`prepare --config configs/eval/hybridqa_dev50_ollama.yaml --out ../benchmark/hybridqa_prepared/dev_s50_ollama` took 3 min 18 s with no embedding errors. It has the same 50 question ids as `dev_s50` (25 table, 25 passage), 50 tables, 1,521 passages (4 skipped, no readable text) and 3,565 chunks; `prepare.json` records `provider: ollama`.

## Tests added
- `test_config.py`: provider default and allowed values.
- `test_doc_ingest.py` (HTTP patched): Ollama request URL, model, batching, prefixes, normalisation; no prefix for other models; `None` on a wrong dimension or a connection error; Gemini still the default and unchanged.
- `test_eval_run.py`: a bundle with another provider raises before any question; a different `base_url` does not; a `prepare.json` without the new keys still matches.
- `test_hybridqa_prepare.py`: `prepare` with an Ollama config records the provider and passes it to the embedding call.

## Commands and results
```
cd db-doc-qa
uv run pytest                                   # 376 passed
ollama pull nomic-embed-text                    # once
uv run python -m dev_test.phase_7_2a            # live: (2, 768) vectors, cosine 0.914 (relevant) vs 0.337
uv run python -m evals.hybridqa prepare --hybridqa ../benchmark/HybridQA \
  --config configs/eval/hybridqa_dev50_ollama.yaml --per-type 25 --seed 20260101 \
  --out ../benchmark/hybridqa_prepared/dev_s50_ollama
```

## Limitations
- Only the embedding backend is switchable; the agent LLM is still Gemini, with its own quota.
- No git commit was made.
