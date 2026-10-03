from __future__ import annotations

import os
import subprocess
from pathlib import Path

from hermes_controller import brainstorm


def test_git_child_env_excludes_worker_secrets_and_git_injection(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/tmp/home-canary")
    monkeypatch.setenv("HERMES_MAIN_LINUX_TOKEN", "worker-token-canary")
    monkeypatch.setenv("HERMES_OPERATOR_TOKEN", "operator-token-canary")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key-canary")
    monkeypatch.setenv("SPIKE_TOKEN_CANARY", "arbitrary-token-canary")
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", "/tmp/evil-diff")
    monkeypatch.setenv("GIT_SSH_COMMAND", "/tmp/evil-ssh")
    monkeypatch.setenv("LD_PRELOAD", "/tmp/evil-preload.so")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "diff.external")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "/tmp/evil-diff")

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, stdout=b"")

    monkeypatch.setattr(brainstorm.subprocess, "run", fake_run)
    brainstorm._git(tmp_path, "status", "--porcelain=v2")

    child_env = seen["kwargs"]["env"]
    assert child_env["PATH"] == "/usr/bin:/bin"
    assert child_env["GIT_OPTIONAL_LOCKS"] == "0"
    assert child_env["GIT_TERMINAL_PROMPT"] == "0"
    assert child_env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert child_env["GIT_CONFIG_GLOBAL"] == os.devnull
    for name in (
        "HERMES_MAIN_LINUX_TOKEN",
        "HERMES_OPERATOR_TOKEN",
        "OPENAI_API_KEY",
        "SPIKE_TOKEN_CANARY",
        "HOME",
        "GIT_EXTERNAL_DIFF",
        "GIT_SSH_COMMAND",
        "LD_PRELOAD",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
    ):
        assert name not in child_env


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_repo_fingerprint_disables_external_diff_helpers(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Hermes Test")
    _git(repo, "config", "user.email", "hermes@example.invalid")
    tracked = repo / "tracked.txt"
    tracked.write_text("before\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "base")

    marker = tmp_path / "external-diff-ran"
    helper = tmp_path / "external-diff.sh"
    helper.write_text(
        "#!/usr/bin/env bash\n"
        f"printf ran > {marker}\n"
        "exit 0\n",
        encoding="utf-8",
    )
    helper.chmod(0o700)
    _git(repo, "config", "diff.external", str(helper))
    tracked.write_text("after\n", encoding="utf-8")

    fingerprint = brainstorm.repo_fingerprint(repo)

    assert fingerprint["head"] != "UNBORN"
    assert not marker.exists()



def test_repo_fingerprint_disables_textconv_helpers(tmp_path):
    repo = tmp_path / "repo-textconv"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Hermes Test")
    _git(repo, "config", "user.email", "hermes@example.invalid")
    (repo / ".gitattributes").write_text("*.bin diff=marker\n", encoding="utf-8")
    tracked = repo / "tracked.bin"
    tracked.write_bytes(b"before\n")
    _git(repo, "add", ".gitattributes", "tracked.bin")
    _git(repo, "commit", "-m", "base")

    marker = tmp_path / "textconv-ran"
    helper = tmp_path / "textconv.sh"
    helper.write_text(
        "#!/usr/bin/env bash\n"
        f"printf ran > {marker}\n"
        "cat \"$1\"\n",
        encoding="utf-8",
    )
    helper.chmod(0o700)
    _git(repo, "config", "diff.marker.textconv", str(helper))
    tracked.write_bytes(b"after\n")

    fingerprint = brainstorm.repo_fingerprint(repo)

    assert fingerprint["head"] != "UNBORN"
    assert not marker.exists()
