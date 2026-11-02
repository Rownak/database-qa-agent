from data_agent.config import AgentConfig, CacheConfig, ResponseCacheConfig
from data_agent.runner import AgentSession, files_state

cfg = AgentConfig(
    latency_trace=True,
    cache=CacheConfig(response=ResponseCacheConfig(enabled=True, scope="dataset")),
)
question = "How many shipments are in the data?"

for label, text in (("miss", question), ("hit ", question.upper())):  # the key is normalised, so case is ignored
    session = AgentSession(cfg, files_state(None, None))
    result = session.ask(text)
    session.close()
    stages = ", ".join(f"{s['stage']} {s['s']:.3f}s" for s in result.timing["stages"])
    print(f"{label}: error={result.error} latency={result.latency_s:.3f}s answer={result.answer[:60]!r}")
    print(f"      cache={result.cache}")
    print(f"      calls={[c['name'] for c in result.calls]} stages=[{stages}]")
