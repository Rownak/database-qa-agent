import json
import time
from pathlib import Path

import yaml

from data_agent import intent

BUNDLE = Path("../benchmark/lakequest_prepared/bank_sample")
PROMPT_FILE = Path("configs/prompts/intent_lakequest.md")
# hand-written, not taken from the benchmark; the key is the label we expect
QUESTIONS = {
    "database": "How many accounts were opened at branch 3 this year?",
    "document": "What are the steps when a customer disputes a card charge?",
    "hybrid": "Client 2043 asks to raise a daily transfer limit. What does the policy require given the account history?",
}

state_file = BUNDLE / "state.json"
state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.is_file() else {}  # real bank summaries if present
lake_prompt = intent.check_prompt(PROMPT_FILE.read_text(encoding="utf-8"))

print(f"{'expected':<10}{'default (shipments) prompt':<30}lakequest prompt")
for expected, question in QUESTIONS.items():
    cells = []
    for prompt in (None, lake_prompt):  # None = the built-in prompt
        started = time.monotonic()
        result = intent.classify(question, state.get("schema_summary"), state.get("docs_summary"), prompt=prompt)
        cells.append(f"{result.intent:<10}{time.monotonic() - started:4.2f}s")
    print(f"{expected:<10}{cells[0]:<30}{cells[1]}")

dataset = BUNDLE / "dataset.yaml"
if dataset.is_file():
    questions = [c["question"].strip().lower() for c in yaml.safe_load(dataset.read_text(encoding="utf-8"))["cases"]]
    print("benchmark questions found verbatim in the prompt file:", sum(q in lake_prompt.lower() for q in questions), "of", len(questions))
else:
    print("LakeQuest bundle not found: verbatim check skipped")
