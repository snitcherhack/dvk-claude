from __future__ import annotations

import json
from pathlib import Path

from hermes_controller.adapters import AdapterResult, HybridAdapter


class SequenceAdapter:
    def __init__(self, results):
        self.results = list(results)
        self.tasks = []

    def execute(self, task):
        self.tasks.append(task)
        return self.results.pop(0)


def task(root: Path):
    work = root / "repo"
    work.mkdir()
    (work / ".git").mkdir()
    task_file = root / "task.md"
    task_file.write_text("Implement the requested change.", encoding="utf-8")
    out = root / "runtime"
    return {
        "brain": {"repository": "brain", "ref": "main", "commit": "a" * 40, "task_file": str(task_file)},
        "project": "test",
        "repository": "repo",
        "ref": "main",
        "task_type": "development",
        "platform": "linux",
        "required_capabilities": ["claude", "codex"],
        "execution_profile": "hermes",
        "execution_engine": "hybrid",
        "human_gates": [],
        "idempotency_policy": "safe_retry",
        "working_directory": str(work),
        "run_output_dir": str(out),
        "allowed_paths": [str(root)],
    }


def done(summary):
    return AdapterResult(status="DONE", summary=summary, completed=[summary], evidence=[f"evidence:{summary}"])


def blocked(summary):
    return AdapterResult(status="BLOCKED", summary=summary, evidence=[f"evidence:{summary}"])


def test_hybrid_passes_after_initial_review(tmp_path):
    spec = task(tmp_path)
    primary = SequenceAdapter([done("implemented")])
    reviewer = SequenceAdapter([done("clean review")])
    result = HybridAdapter(primary, reviewer, max_review_rounds=1).execute(spec)

    assert result.status == "DONE"
    assert len(primary.tasks) == 1
    assert len(reviewer.tasks) == 1
    assert primary.tasks[0]["execution_engine"] == "claude"
    assert reviewer.tasks[0]["execution_engine"] == "codex"
    assert reviewer.tasks[0]["execution_profile"] == "review"
    report = Path(spec["run_output_dir"]) / "hybrid-summary.json"
    data = json.loads(report.read_text(encoding="utf-8"))
    assert [stage["stage"] for stage in data["stages"]] == ["primary", "review-0"]


def test_hybrid_runs_one_fix_round_then_reviews_again(tmp_path):
    spec = task(tmp_path)
    primary = SequenceAdapter([done("implemented"), done("fixed")])
    reviewer = SequenceAdapter([blocked("bug found"), done("clean")])
    result = HybridAdapter(primary, reviewer, max_review_rounds=1).execute(spec)

    assert result.status == "DONE"
    assert len(primary.tasks) == 2
    assert len(reviewer.tasks) == 2
    assert primary.tasks[1]["brain"]["task_file"].endswith("fix-1.md")
    assert reviewer.tasks[1]["brain"]["task_file"].endswith("review-1.md")
    assert "bug found" in Path(primary.tasks[1]["brain"]["task_file"]).read_text(encoding="utf-8")


def test_hybrid_blocks_when_findings_remain_after_limit(tmp_path):
    spec = task(tmp_path)
    primary = SequenceAdapter([done("implemented"), done("fixed")])
    reviewer = SequenceAdapter([blocked("bug one"), blocked("bug remains")])
    result = HybridAdapter(primary, reviewer, max_review_rounds=1).execute(spec)

    assert result.status == "BLOCKED"
    assert "still has material findings" in result.summary


def test_hybrid_propagates_primary_terminal_state(tmp_path):
    spec = task(tmp_path)
    primary = SequenceAdapter([AdapterResult(status="WAIT_USER", summary="approval required", gate="LICENSE_REVIEW_REQUIRED")])
    reviewer = SequenceAdapter([])
    result = HybridAdapter(primary, reviewer).execute(spec)

    assert result.status == "WAIT_USER"
    assert result.gate == "LICENSE_REVIEW_REQUIRED"
    assert reviewer.tasks == []
