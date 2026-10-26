"""Phase 5.1: prints the LakeQuest metrics for two hand-made results and the start of the lakequest instruction."""
from data_agent.agent import LAKE_INSTRUCTION
from data_agent.events import TurnResult
from evals.dataset import EvalCase, Expected
from evals.metrics import METRICS

case = EvalCase(
    id="demo",
    question="Can client 748 close the account?",
    expected=Expected(reference_answers=["Yes, after identity checks."], object_ids=["deceased-estate-servicing", "client"]),
)
passage = {"source": "deceased-estate-servicing.md", "section_number": "5", "text": "t"}
calls = [
    {"name": "run_sql", "args": {"query": 'SELECT * FROM "client" WHERE "client_id" = 748'}, "response": {"status": "success"}},
    {"name": "search_documents", "args": {}, "response": {"status": "success", "passages": [passage]}},
]
good = TurnResult("Yes, after identity checks. [deceased-estate-servicing.md, §5 Safeguards]", calls, None, 1.0)
bad = TurnResult("", [], None, 1.0)

for label, result in (("good", good), ("empty", bad)):
    print(label, {name: METRICS[name](case, result) for name in ("answer_f1", "object_f1", "coverage")})
print(LAKE_INSTRUCTION[:300])
