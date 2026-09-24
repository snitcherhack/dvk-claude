from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from hermes_controller import adapters
from hermes_controller.adapters import CodexRunAdapter, StructuredExecutionError, StructuredExecutionResult
from hermes_controller.brainstorm_core import STAGE_SCHEMAS, canonical_json

REPO = Path(__file__).parents[1]
RUNNER = REPO / "hermes-codex-run.sh"
SCHEMA_DIR = REPO / "hermes_controller" / "schemas" / "brainstorm"
PAYLOAD = {"proposals": [{"title": "A", "concept": "B"}], "extra": {"status": "not-hermes"}}
IGNORED_ENV = {"PWD", "SHLVL", "_", "OLDPWD"}
ALLOWED_CODEX_ENV = {"HOME", "PATH", "LANG", "CODEX_HOME", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "ALL_PROXY",
                     "https_proxy", "http_proxy", "no_proxy", "all_proxy"}

# Fake Codex CLI. It cannot receive its mode through the environment because the
# runner launches it with `env -i`, so mode and payload live next to it on disk.
FAKE_CODEX = r"""#!/bin/bash
FAKE_DIR="__FAKE_DIR__"
mode="$(cat "$FAKE_DIR/mode" 2>/dev/null || echo pass)"
sub="$1"; shift
{
  printf 'CALL %s\n' "$sub"
  for a in "$@"; do printf 'ARG %s\n' "$a"; done
  env | cut -d= -f1 | sort | sed 's/^/ENV /'
  printf 'END\n'
} >> "$FAKE_DIR/calls.log"
case "$sub" in
  --version) echo "codex-cli 0.154.0-fake"; exit 0 ;;
  sandbox)
    spec="${@: -1}"
    cp "$spec" "$FAKE_DIR/spec.copy"
    while IFS=$'\t' read -r kind path _rest; do
      [ -z "$kind" ] && continue
      if [ "$mode" = "probe-fail" ] && [ "$kind" = "HIDDEN" ]; then echo "FAIL $kind $path"; else echo "OK $kind $path"; fi
    done < "$spec"
    [ "$mode" = "probe-crash" ] && exit 1
    exit 0 ;;
  exec)
    out=""; prev=""
    for a in "$@"; do [ "$prev" = "--output-last-message" ] && out="$a"; prev="$a"; done
    case "$mode" in
      slow) sleep 10 ;;
      invalid) echo "not json" > "$out"; exit 0 ;;
      array) echo "[1, 2]" > "$out"; exit 0 ;;
      fail) exit 1 ;;
    esac
    cat "$FAKE_DIR/payload.json" > "$out"
    exit 0 ;;
esac
exit 64
"""


@dataclass
class Env:
    root: Path
    home: Path
    fake: Path
    repo: Path
    job: Path
    stage: Path
    sibling: Path
    orchestration: Path

    def mode(self, value: str) -> None:
        (self.fake / "mode").write_text(value, encoding="utf-8")

    def calls(self) -> list[dict]:
        log = self.fake / "calls.log"
        if not log.exists():
            return []
        calls, current = [], None
        for line in log.read_text(encoding="utf-8").splitlines():
            if line.startswith("CALL "):
                current = {"sub": line[5:], "args": [], "env": set()}
            elif line.startswith("ARG ") and current is not None:
                current["args"].append(line[4:])
            elif line.startswith("ENV ") and current is not None:
                current["env"].add(line[4:])
            elif line == "END" and current is not None:
                calls.append(current)
                current = None
        return calls

    def spec(self) -> list[list[str]]:
        return [line.split("\t") for line in (self.fake / "spec.copy").read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture
def env(tmp_path, monkeypatch) -> Env:
    home = tmp_path / "home"
    packages_bin = home / ".codex" / "packages" / "bin"
    packages_bin.mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text("{}", encoding="utf-8")  # dummy, not a credential
    (home / ".codex" / "sessions").mkdir()
    fake = tmp_path / "fake"
    fake.mkdir()
    codex = packages_bin / "codex"
    codex.write_text(FAKE_CODEX.replace("__FAKE_DIR__", str(fake)), encoding="utf-8")
    codex.chmod(0o755)
    (fake / "payload.json").write_text(json.dumps(PAYLOAD), encoding="utf-8")

    repo = tmp_path / "projects" / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "README.md").write_text("repo", encoding="utf-8")
    job = tmp_path / "runtime" / "job-1"
    stage = job / "stages" / "codex-proposals"
    sibling = job / "stages" / "claude-proposals"
    orchestration = job / "orchestration"
    for path in (stage / "input", sibling, orchestration):
        path.mkdir(parents=True)
    (stage / "task.md").write_text("Propose formats.", encoding="utf-8")

    for name in list(os.environ):
        if name.lower().endswith("_proxy") or name == "CODEX_HOME":
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_CODEX_CLI", str(codex))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("HERMES_MAIN_LINUX_TOKEN", "worker-token-canary")
    monkeypatch.setenv("OPENAI_API_KEY", "api-key-canary")
    monkeypatch.setenv("SPIKE_TOKEN_CANARY", "env-canary")
    return Env(tmp_path, home, fake, repo, job, stage, sibling, orchestration)


def brainstorm_task(e: Env, **overrides) -> dict:
    spec = {
        "task_type": "brainstorm",
        "execution_profile": "brainstorm",
        "working_directory": str(e.repo),
        "brain": {"task_file": str(e.stage / "task.md")},
        "run_output_dir": str(e.stage),
        "timeout_seconds": 1800,
        "brainstorm_probe": {"hidden_paths": [str(e.sibling), str(e.orchestration)]},
    }
    spec.update(overrides)
    return spec


def make_adapter(e: Env, *, profiles=None, types=None) -> CodexRunAdapter:
    return CodexRunAdapter(
        runner_path=RUNNER,
        authorized_roots=[e.root / "projects", e.root / "runtime"],
        execution_profiles=profiles or {"hermes", "review", "brainstorm"},
        task_types=types or {"development", "hermes_smoke", "brainstorm"},
    )


def run(e: Env, stage="proposals", roots=None, timeout=60, **task_overrides) -> StructuredExecutionResult:
    return make_adapter(e).execute_structured(
        brainstorm_task(e, **task_overrides), stage_schema=stage,
        stage_roots=roots if roots is not None else [str(e.repo), str(e.stage)], timeout_seconds=timeout,
    )


def permission_entries(call: dict) -> dict[str, str]:
    value = next(arg for arg in call["args"] if arg.startswith("permissions.hermes_brainstorm.filesystem="))
    body = value.split("=", 1)[1].strip()
    assert body.startswith("{") and body.endswith("}")
    entries = {}
    for item in body[1:-1].split(", "):
        key, _, mode = item.partition("=")
        entries[json.loads(key)] = json.loads(mode)
    return entries


# --- versioned schemas -------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(STAGE_SCHEMAS))
def test_versioned_schema_files_equal_stage_schemas(name):
    stored = json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))
    assert canonical_json(stored) == canonical_json(STAGE_SCHEMAS[name])


def test_schema_directory_holds_exactly_the_four_stage_schemas():
    assert sorted(path.name for path in SCHEMA_DIR.iterdir()) == sorted(f"{name}.schema.json" for name in STAGE_SCHEMAS)


# --- success path --------------------------------------------------------------------------------

def test_structured_stage_returns_payload_untransformed(env):
    result = run(env)
    assert isinstance(result, StructuredExecutionResult)
    assert result.stage == "proposals"
    assert result.payload == PAYLOAD
    assert not hasattr(result, "status")
    stored = env.stage / "codex-structured.json"
    assert json.loads(stored.read_text(encoding="utf-8")) == PAYLOAD
    assert str(stored) in result.evidence
    assert str(env.stage / "isolation-probe.json") in result.evidence
    assert result.metadata["isolation_probe"] == "PASS"


@pytest.mark.parametrize("stage", sorted(STAGE_SCHEMAS))
def test_exec_uses_versioned_schema_by_name_and_isolated_flags(env, stage):
    run(env, stage=stage)
    calls = env.calls()
    assert [call["sub"] for call in calls] == ["sandbox", "exec"]
    args = calls[1]["args"]
    assert args[args.index("--output-schema") + 1] == str(SCHEMA_DIR / f"{stage}.schema.json")
    for flag in ("--ephemeral", "--ignore-user-config", "--ignore-rules", "--strict-config"):
        assert flag in args
    assert "--sandbox" not in args and "-s" not in args
    assert "--profile" not in args and "-p" not in args
    assert 'approval_policy="never"' in args
    assert 'default_permissions="hermes_brainstorm"' in args
    last_message = Path(args[args.index("--output-last-message") + 1])
    assert env.stage not in last_message.parents and env.repo not in last_message.parents


def test_probe_and_model_use_the_same_permission_profile(env):
    run(env)
    probe, model = env.calls()
    assert permission_entries(probe) == permission_entries(model)
    assert 'default_permissions="hermes_brainstorm"' in probe["args"]
    # Codex 0.154 requires --permission-profile with `sandbox -C`, which conflicts
    # with default_permissions; the probe must select the profile like exec does.
    assert "-C" not in probe["args"] and "--permission-profile" not in probe["args"]


def test_permission_profile_contains_only_declared_roots(env):
    run(env)
    entries = permission_entries(env.calls()[1])
    assert entries == {
        ":minimal": "read",
        str(env.home / ".codex" / "packages"): "read",
        str(env.repo): "read",
        str(env.stage): "write",
    }
    assert not any(str(env.home / ".codex" / "auth.json") in key or key == str(env.home / ".codex") for key in entries)


# --- environment -----------------------------------------------------------------------------------

def test_codex_gets_only_positive_env_allow_list(env):
    run(env)
    for call in env.calls():
        visible = call["env"] - IGNORED_ENV
        assert visible <= ALLOWED_CODEX_ENV
        assert {"HOME", "PATH", "LANG"} <= visible
        assert not {"HERMES_MAIN_LINUX_TOKEN", "OPENAI_API_KEY", "SPIKE_TOKEN_CANARY", "HERMES_CODEX_CLI",
                    "XDG_STATE_HOME"} & visible


def test_runner_process_does_not_inherit_worker_environment(env, monkeypatch):
    seen = []
    real_popen = adapters.subprocess.Popen
    monkeypatch.setattr(adapters.subprocess, "Popen",
                        lambda argv, **kw: (seen.append((argv, kw)) or real_popen(argv, **kw)))
    run(env)
    passed = seen[0][1]["env"]
    assert "HERMES_MAIN_LINUX_TOKEN" not in passed and "OPENAI_API_KEY" not in passed
    assert "SPIKE_TOKEN_CANARY" not in passed
    assert set(passed) <= {"HOME", "PATH", "LANG", "HERMES_CODEX_CLI", "CODEX_HOME", "XDG_STATE_HOME"} | ALLOWED_CODEX_ENV


def test_proxy_without_credentials_is_passed_by_name(env, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1056")
    run(env)
    assert "HTTPS_PROXY" in env.calls()[1]["env"]


def test_proxy_with_credentials_is_blocked(env, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://user:pass@127.0.0.1:1056")
    with pytest.raises(StructuredExecutionError) as info:
        run(env)
    assert info.value.status == "BLOCKED"
    assert env.calls() == []


# --- isolation probe ------------------------------------------------------------------------------------

def test_probe_spec_covers_required_checks(env):
    run(env)
    spec = env.spec()
    kinds = {(row[0], row[1]) for row in spec}
    assert ("READ", str(env.repo)) in kinds
    assert ("NOWRITE", str(env.repo)) in kinds
    assert ("RW", str(env.stage)) in kinds
    assert ("NET", "-") in kinds
    assert ("ENV", "-") in kinds
    hidden = {row[1] for row in spec if row[0] == "HIDDEN"}
    for path in (env.home / ".codex" / "auth.json", env.home / ".codex" / "sessions", env.home / ".ssh",
                 env.home / ".claude", env.home / ".config" / "dvk-hermes", Path("/mnt/c"), env.sibling, env.orchestration):
        assert str(path) in hidden
    assert any("outside-canary" in path for path in hidden)
    skeleton = {row[1]: set(row[2:]) for row in spec if row[0] == "SKELETON"}
    assert skeleton[str(env.stage.parent)] == {env.stage.name}
    assert skeleton[str(env.job)] == {"stages"}
    assert skeleton[str(env.repo.parent)] == {env.repo.name}
    assert skeleton[str(env.home)] == {".codex"}
    assert skeleton[str(env.home / ".codex")] == {"packages"}


@pytest.mark.parametrize("mode", ["probe-fail", "probe-crash"])
def test_failed_probe_blocks_without_running_the_model(env, mode):
    env.mode(mode)
    with pytest.raises(StructuredExecutionError, match="isolation probe") as info:
        run(env)
    assert info.value.status == "BLOCKED"
    assert [call["sub"] for call in env.calls()] == ["sandbox"]
    report = json.loads((env.stage / "isolation-probe.json").read_text(encoding="utf-8"))
    assert report["status"] == "FAIL"
    assert not (env.stage / "codex-structured.json").exists()


# --- schema and argument validation ------------------------------------------------------------------------

@pytest.mark.parametrize("stage_schema", ["report", "", "../proposals", str(SCHEMA_DIR / "proposals.schema.json"),
                                          {"type": "object"}, None])
def test_unknown_or_arbitrary_schema_is_blocked_before_runner(env, stage_schema):
    with pytest.raises(StructuredExecutionError, match="stage schema") as info:
        make_adapter(env).execute_structured(brainstorm_task(env), stage_schema=stage_schema,
                                             stage_roots=[str(env.repo), str(env.stage)], timeout_seconds=60)
    assert info.value.status == "BLOCKED"
    assert env.calls() == []


def runner_cli(e: Env, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(RUNNER), *args], text=True, capture_output=True, check=False)


@pytest.mark.parametrize("schema", ["../proposals", "/etc/passwd", str(SCHEMA_DIR / "proposals.schema.json"),
                                    "proposals.schema.json", "PROPOSALS", '{"type":"object"}'])
def test_runner_rejects_schema_paths_and_arbitrary_values(env, schema):
    result = runner_cli(env, "--execution-profile", "brainstorm", "--stage-schema", schema,
                        "--working-directory", str(env.repo), "--task-file", str(env.stage / "task.md"),
                        "--run-output-dir", str(env.stage), "--allowed-path", str(env.repo),
                        "--allowed-path", str(env.stage), "--timeout-seconds", "60")
    assert result.returncode == 2
    assert env.calls() == []


def test_runner_rejects_stage_schema_outside_brainstorm_profile(env):
    result = runner_cli(env, "--execution-profile", "hermes", "--stage-schema", "proposals",
                        "--working-directory", str(env.repo), "--task-file", str(env.stage / "task.md"),
                        "--run-output-dir", str(env.stage), "--allowed-path", str(env.repo),
                        "--allowed-path", str(env.stage))
    assert result.returncode == 2
    assert env.calls() == []


def test_runner_requires_stage_schema_for_brainstorm(env):
    result = runner_cli(env, "--execution-profile", "brainstorm",
                        "--working-directory", str(env.repo), "--task-file", str(env.stage / "task.md"),
                        "--run-output-dir", str(env.stage), "--allowed-path", str(env.repo),
                        "--allowed-path", str(env.stage), "--timeout-seconds", "60")
    assert result.returncode == 2
    assert env.calls() == []


def test_root_exposing_codex_home_is_blocked(env):
    with pytest.raises(StructuredExecutionError) as info:
        CodexRunAdapter(runner_path=RUNNER, authorized_roots=[env.root],
                        execution_profiles={"brainstorm"}, task_types={"brainstorm"},
                        ).execute_structured(brainstorm_task(env), stage_schema="proposals",
                                             stage_roots=[str(env.repo), str(env.stage), str(env.home)],
                                             timeout_seconds=60)
    assert info.value.status == "BLOCKED"
    assert env.calls() == []


@pytest.mark.parametrize("roots_factory, message", [
    (lambda e: [], "stage_roots"),
    (lambda e: [str(e.repo)], "run_output_dir"),
    (lambda e: [str(e.repo), str(e.job)], "run_output_dir"),
    (lambda e: [str(e.stage)], "working_directory"),
    (lambda e: [str(e.repo), str(e.stage), "/etc"], "outside authorized roots"),
])
def test_invalid_stage_roots_are_blocked(env, roots_factory, message):
    with pytest.raises(StructuredExecutionError, match=message) as info:
        run(env, roots=roots_factory(env))
    assert info.value.status == "BLOCKED"
    assert env.calls() == []


def test_brainstorm_must_be_enabled_and_is_not_available_through_execute(env):
    with pytest.raises(StructuredExecutionError, match="execution_profile"):
        make_adapter(env, profiles={"hermes"}).execute_structured(
            brainstorm_task(env), stage_schema="proposals", stage_roots=[str(env.repo), str(env.stage)],
            timeout_seconds=60)
    result = make_adapter(env).execute(brainstorm_task(env))
    assert result.status == "BLOCKED"
    assert "execute_structured" in result.summary
    assert env.calls() == []


# --- timeout, output errors and output-path safety -------------------------------------------------------------

def test_effective_timeout_is_passed_to_runner_and_enforced(env, monkeypatch):
    seen = []
    real_popen = adapters.subprocess.Popen
    monkeypatch.setattr(adapters.subprocess, "Popen",
                        lambda argv, **kw: (seen.append(argv) or real_popen(argv, **kw)))
    env.mode("slow")
    with pytest.raises(StructuredExecutionError, match="timed out") as info:
        run(env, timeout=2)
    assert info.value.status == "FAILED"
    argv = seen[0]
    assert argv[argv.index("--timeout-seconds") + 1] == "2"


@pytest.mark.parametrize("timeout", [0, 7201, True, "60", None])
def test_invalid_effective_timeout_is_blocked(env, timeout):
    with pytest.raises(StructuredExecutionError, match="timeout_seconds"):
        run(env, timeout=timeout)


@pytest.mark.parametrize("mode, message", [("invalid", "JSON object"), ("array", "JSON object"), ("fail", "exited")])
def test_bad_structured_output_fails(env, mode, message):
    env.mode(mode)
    with pytest.raises(StructuredExecutionError, match=message) as info:
        run(env)
    assert info.value.status == "FAILED"


def test_preexisting_symlink_at_result_path_is_replaced_not_followed(env):
    outside_target = env.root / "outside-target.txt"
    outside_target.write_text("untouched", encoding="utf-8")
    (env.stage / "codex-structured.json").symlink_to(outside_target)
    (env.stage / "codex-exec.log").symlink_to(outside_target)
    result = run(env)
    assert outside_target.read_text(encoding="utf-8") == "untouched"
    assert not (env.stage / "codex-structured.json").is_symlink()
    assert result.payload == PAYLOAD


def test_existing_codex_profiles_keep_runner_contract(env):
    result = runner_cli(env, "--execution-profile", "review", "--dry-run",
                        "--working-directory", str(env.repo), "--task-file", str(env.stage / "task.md"),
                        "--run-output-dir", str(env.stage), "--allowed-path", str(env.repo),
                        "--allowed-path", str(env.stage))
    assert result.returncode == 0
    assert "--sandbox workspace-write" in result.stdout
