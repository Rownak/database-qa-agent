# Git Workflow — Database Q&A Agent

A simple branching workflow for building the MVP in [sprint1.md](sprint1.md).

| Branch | Purpose |
|---|---|
| `main` | Stable, demo-ready code only. It changes only through a merge from `dev`. |
| `dev` | Integration branch. Each finished phase is merged here. |
| `phase/<n>-<name>` | One short-lived branch per sprint phase, created from `dev`. |

```
main ──●────────────────────────────────────────●── v0.1.0
        \                                      /
dev      ●───●──────●──────●──────●──────●───●
              \    / \    / \    / ...
phase/1-setup  ●──●   ●──●   ●──●
```

---

## 1. One-Time Setup

```bash
mkdir database-qa-agent
cd database-qa-agent
git init -b main

# Copy the planning documents into the repository
cp <old-path>/project_description.md <old-path>/design.md <old-path>/sprint1.md .

# Ignore secrets, the virtual environment, and caches before the first commit
printf ".env\n.venv/\n__pycache__/\n*.db\n" > .gitignore

git add .
git commit -m "docs: add project description, design, and sprint 1 plan"

# Create the integration branch
git switch -c dev
```

**Optional: push to GitHub.**

```bash
gh repo create database-qa-agent --private --source . --remote origin
git push -u origin main
git push -u origin dev
```

> **Tip:** this repository *is* the project. In Phase 1, run `uv init --app --python 3.12 .` at the repository root instead of creating a `spreadsheet-qa/` subfolder. The `--no-workspace` flag is not needed here.

## 2. Phase Loop (repeat for each phase)

| Phase | Branch name |
|---|---|
| 1 | `phase/1-setup` |
| 2 | `phase/2-ingestion` |
| 3 | `phase/3-tools` |
| 4 | `phase/4-agent` |
| 5 | `phase/5-ui-sessions` |
| 6 | `phase/6-verification` |

```bash
# 1. Start the phase from the latest dev
git switch dev
git pull                      # only if a remote is set up
git switch -c phase/1-setup

# 2. Work in small commits as you check off tasks in sprint1.md
git add .
git commit -m "feat: init uv project and add dependencies"
git commit -am "chore: add sample data files"

# 3. Before merging, run the phase's "Quick check" from sprint1.md
#    and tick its checklist in sprint1.md
git commit -am "docs: mark phase 1 complete"

# 4. Merge into dev, keeping the phase visible in history
git switch dev
git merge --no-ff phase/1-setup -m "merge: phase 1 - uv project setup"
git push                      # only if a remote is set up

# 5. Delete the finished phase branch
git branch -d phase/1-setup
```

**Using pull requests instead of local merges (optional):** push the phase branch with `git push -u origin phase/1-setup`, then open a pull request with `gh pr create --base dev --fill` and merge it with `gh pr merge --merge --delete-branch`.

## 3. Release: Merge `dev` into `main`

Do this only after Phase 6 has passed every verification checkbox.

```bash
git switch main
git merge --no-ff dev -m "release: Database Q&A Agent MVP v0.1.0"
git tag -a v0.1.0 -m "First working demo"
git push origin main --tags   # only if a remote is set up
git switch dev                # keep developing on dev
```

## 4. Conventions

- **Commit prefixes:** `feat:` new functionality, `fix:` bug fixes, `docs:` documentation, `chore:` setup and configuration, `test:` checks and tests.
- **Never commit** `.env`, `.venv/`, or `*.db` files. They are already in `.gitignore`.
- **Keep `main` stable:** don't commit to it directly. Fixes go through `dev`; for a fix on `dev`, use a `fix/<topic>` branch.
- **If a merge conflict happens:** fix the files, then run `git add <file>` and `git commit` to finish the merge.
- **Check where you are** with `git status` and `git log --oneline --graph --all`.


## Revert push

Run these from the repo root. Check first, because the right undo depends on whether you pushed.

```bash
git switch main
git log --oneline --graph -5     # the top commit should be "release: Database Q&A Agent MVP v0.1.0"
git tag                          # v0.1.0 should be listed
```

**1. Undo locally**
```bash
git tag -d v0.1.0
git reset --hard HEAD^           # moves main back to the commit before the merge
```
`HEAD^` is main's previous tip (the merge's first parent). If the log in step 0 shows anything else on top, stop and use that commit's hash instead.

**2. Undo on the remote, only if you ran the push**
```bash
git push origin :refs/tags/v0.1.0          # delete the remote tag
git push --force-with-lease origin main    # rewind remote main
```
This rewrites the remote `main`. It's fine for a solo demo. If anyone else has pulled it, use `git revert -m 1 <merge-hash>` instead of the reset and force-push. Be aware that re-merging `dev` later then needs you to revert that revert first.

**3. Update the README, then redo the release**
```bash
git switch dev
# edit README.md
git add README.md
git commit -m "docs: update README"

git switch main
git merge --no-ff dev -m "release: Database Q&A Agent MVP v0.1.0"
git tag -a v0.1.0 -m "First working demo"
git push origin main --tags
```

**Shortcut:** you can skip the undo. Commit the README on `dev`, merge it into `main`, then move the tag with `git tag -f -a v0.1.0 -m "First working demo"`. If the tag was pushed, follow with `git push -f origin v0.1.0`. This leaves the earlier merge in history, which is harmless.

