from google.adk.evaluation.eval_case import IntermediateData, Invocation
from google.adk.evaluation.eval_metrics import BaseCriterion, EvalMetric
from google.adk.evaluation.eval_rubrics import Rubric, RubricContent
from google.genai import types

from evals.adk import metrics

PASSAGE = {"source": "shipment_delay_policy.pdf", "section_number": "3.2", "text": "Express: 2 days."}
ANSWER = "Express shipments escalate after 2 days [shipment_delay_policy.pdf, §3.2 Express Shipments, p. 2]."


def content(role, text):
    return types.Content(role=role, parts=[types.Part(text=text)])


rubrics = [
    Rubric(rubric_id="c1", rubric_content=RubricContent(text_property="2 days"), type=metrics.ANSWER_CONTAINS),
    Rubric(rubric_id="e1", rubric_content=RubricContent(text_property="never"), type=metrics.ANSWER_EXCLUDES),
    Rubric(rubric_id="s1", rubric_content=RubricContent(text_property="shipment_delay_policy.pdf|3.2"), type=metrics.EXPECTED_SECTION),
]
golden = Invocation(user_content=content("user", "q"), final_response=content("model", ""), rubrics=rubrics)
agent = Invocation(
    user_content=content("user", "What is the express threshold?"),
    final_response=content("model", ANSWER),
    intermediate_data=IntermediateData(
        tool_uses=[types.FunctionCall(id="1", name="search_documents", args={"query": "express"})],
        tool_responses=[types.FunctionResponse(id="1", name="search_documents", response={"status": "success", "passages": [PASSAGE]})],
    ),
)
for name in ("answer_contains", "answer_excludes", "section_retrieved", "citation_grounded"):
    metric = EvalMetric(metric_name=name, threshold=1.0, criterion=BaseCriterion(threshold=1.0))
    result = getattr(metrics, name)(metric, [agent], [golden])
    print(f"{name}: {result.overall_eval_status.name} score={result.overall_score}")
