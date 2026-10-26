"""Phase 4.2: asks one question through AgentSession with the real model (needs GOOGLE_API_KEY)."""
from data_agent.config import AgentConfig
from data_agent.runner import AgentSession, files_state

session = AgentSession(AgentConfig(), files_state(None, None))
result = session.ask("Where is shipment 12345?")
print("answer :", result.answer)
print("intent :", result.intent)
print("tools  :", [c["name"] for c in result.calls])
print("latency: %.1fs" % result.latency_s, "| error:", result.error)
session.close()
