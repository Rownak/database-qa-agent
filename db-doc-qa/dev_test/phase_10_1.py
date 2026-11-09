from pathlib import Path

from google.adk.evaluation.eval_set import EvalSet

ADK_DIR = Path(__file__).resolve().parent.parent / "evals" / "adk"

for folder in ("unit_routing", "unit_hybrid"):
    eval_set = EvalSet.model_validate_json((ADK_DIR / folder / f"{folder}.test.json").read_text(encoding="utf-8"))
    print(f"[{folder}] {eval_set.eval_set_id}")
    for case in eval_set.eval_cases:
        inv = case.conversation[0]
        tools = " -> ".join(t.name for t in inv.intermediate_data.tool_uses) or "(none)"
        print(f"  {case.eval_id}: {inv.user_content.parts[0].text!r}  =>  {tools}")
