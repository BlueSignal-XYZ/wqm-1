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
FORBIDDEN_TOOLS = (
    r"\bWrite\b|\bEdit\b|git push|git commit|gh pr merge|gh pr review"
    r"|gh api|Bash\(\*|Bash\(gh:\*"
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
    assert len(tools) == 2
    for t in tools:
        assert not re.search(FORBIDDEN_TOOLS, t), t


def test_prompts_forbid_approving_merging_and_pushing():
    assert "Never approve, merge, push or change files." in SRC
    assert "Do not change any file. Do not open a PR." in SRC


def test_concurrency_is_per_job():
    assert "concurrency" not in DOC
    assert DOC["jobs"]["plan"]["concurrency"]["group"].startswith("claude-plan-")
    assert DOC["jobs"]["review"]["concurrency"]["group"].startswith("claude-review-")
