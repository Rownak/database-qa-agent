from evals.adk import build

result = build.build()
build.write(result.files)
for suite in result.suites:
    variants = sum(len(c.variants) for c in suite.cases)
    print(f"{suite.suite}: {len(suite.cases)} cases + {variants} variants ({suite.match})")
for message in result.warnings:
    print(f"warning: {message}")
print()
print(build.format_matrix(result.suites))
print("stale files after build:", build.stale_files(result.files))
