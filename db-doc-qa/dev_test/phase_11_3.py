from evals.adk import build

result = build.build()
build.write(result.files)
for suite in result.suites:
    print(f"\n[{suite.suite}] {suite.match}")
    for case in suite.cases:
        flag = "*" if case.critical else " "
        print(f" {flag} {case.id}  layer={case.layer.value} rules={case.rules} state={case.state} variants={len(case.variants)}")
        for n, turn in enumerate(case.turns, 1):
            tools = " -> ".join(c.name + (str(c.args) if c.args else "") for c in turn.tools) or "(none)"
            print(f"      turn {n}: {tools}")
for message in result.warnings:
    print(f"warning: {message}")
