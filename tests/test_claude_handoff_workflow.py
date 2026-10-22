"""The Grok -> Claude handoff (AGENTS.md, "AI staff").

Claude in this workflow may read the repo and write comments and labels.
It may never push, approve or merge, and only the `needs-plan` and
`needs-review` labels start it. Same file and same rules as the
marketplace repo's src/tests/claudeHandoff.test.js.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
FILE = ROOT / ".github" / "workflows" / "claude-handoff.yml"
SRC = FILE.read_text()
DOC = yaml.safe_load(SRC)
# PyYAML reads the bare key `on:` as the boolean True.
ON = DOC.get("on", DOC.get(True))
# The exact allow-lists. A blocklist missed `Bash(git checkout:*)` (review of
# marketplace #1449); comparing the whole string makes any new tool a visible
# edit to this file.
PLAN_TOOLS = (
    "Read,Grep,Glob,Bash(gh issue view:*),Bash(gh issue comment:*),"
    "Bash(gh issue edit:*),Bash(git log:*),Bash(git show:*)"
)
REVIEW_TOOLS = (
    "Read,Grep,Glob,Bash(gh pr view:*),Bash(gh pr diff:*),Bash(gh pr checks:*),"
    "Bash(gh pr comment:*),Bash(gh pr edit:*),Bash(gh issue view:*),"
    "Bash(gh run view:*),Bash(gh run list:*),Bash(git log:*),Bash(git show:*),"
    "Bash(git diff:*)"
)


def test_starts_only_on_a_label():
    assert set(ON) == {"issues", "pull_request"}
    assert ON["issues"] == {"types": ["labeled"]}
    assert ON["pull_request"] == {"types": ["labeled"]}


def test_each_job_runs_only_for_its_own_label():
    assert "github.event.label.name == 'needs-plan'" in DOC["jobs"]["plan"]["if"]
    assert "github.event.label.name == 'needs-review'" in DOC["jobs"]["review"]["if"]


def test_can_never_write_code():
    for job in DOC["jobs"].values():
        assert job["permissions"]["contents"] == "read"
    tools = re.findall(r'--allowedTools "([^"]+)"', SRC)
    assert tools == [PLAN_TOOLS, REVIEW_TOOLS]


def test_checkout_keeps_the_token_out_of_git_config():
    for job in DOC["jobs"].values():
        checkout = next(s for s in job["steps"] if s.get("uses", "").startswith("actions/checkout"))
        assert checkout["with"]["persist-credentials"] is False


def test_review_can_read_ci_and_verdicts_never_stack():
    assert DOC["jobs"]["review"]["permissions"]["checks"] == "read"
    assert (
        "--remove-label needs-review --remove-label changes-requested --add-label review-passed"
        in SRC
    )
    assert (
        "--remove-label needs-review --remove-label review-passed --add-label changes-requested"
        in SRC
    )


def test_prompts_forbid_approving_merging_and_pushing():
    assert "Never approve, merge, push or change files." in SRC
    assert "Do not change any file. Do not open a PR." in SRC


def test_concurrency_is_per_job():
    assert "concurrency" not in DOC
    assert DOC["jobs"]["plan"]["concurrency"]["group"].startswith("claude-plan-")
    assert DOC["jobs"]["review"]["concurrency"]["group"].startswith("claude-review-")
