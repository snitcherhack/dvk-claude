import json
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_controller.controller import Controller, ControllerError, ENGINE_CAPABILITIES
from test_project_registry import manifest


@pytest.fixture
def controller(tmp_path):
    instance = Controller(tmp_path / 'controller')
    instance.register_project(manifest(tmp_path, allowed_integrations=['github_api'], repository='git@github.com:o/r.git'))
    yield instance
    instance.close()


@pytest.mark.parametrize('allowed', [[], ['github_api']])
def test_allowed_integrations_valid(tmp_path, allowed):
    Controller._validate_project(manifest(tmp_path, allowed_integrations=allowed))


@pytest.mark.parametrize('allowed', [None, 'github_api', {}, ['other'], ['github_api', 'github_api'], [1], [[]]])
def test_allowed_integrations_invalid(tmp_path, allowed):
    with pytest.raises(ControllerError):
        Controller._validate_project(manifest(tmp_path, allowed_integrations=allowed))


def test_manifest_cannot_grant_integration_via_capabilities(tmp_path):
    with pytest.raises(ControllerError):
        Controller._validate_project(manifest(tmp_path, capabilities=['github_api']))


@pytest.mark.parametrize('verb', ['pr_create', 'pr_get', 'pr_list', 'actions_status', 'checks_status'])
def test_opt_in(controller, verb):
    request = {'verb': verb, 'params': {}}
    task = controller.build_project_task('sample-project', 'Inspect.', integrations=['github_api'], integration_request=request)
    assert task['integrations'] == ['github_api']
    assert task['integration_request'] == request
    assert 'github_api' in task['required_capabilities']
    assert 'github_api' not in ENGINE_CAPABILITIES
    normal = controller.build_project_task('sample-project', 'Inspect.')
    assert not normal.get('integrations')
    assert 'integration_request' not in normal
    assert 'github_api' not in normal['required_capabilities']


@pytest.mark.parametrize('payload', [None, {}, [], {'verb': 'merge', 'params': {}}, {'verb': 'push', 'params': {}}, {'verb': 'unknown', 'params': {}}, {'verb': [], 'params': {}}, {'verb': 'pr_get'}, {'verb': 'pr_get', 'params': []}])
def test_invalid_request(controller, payload):
    with pytest.raises(ControllerError):
        controller.build_project_task('sample-project', 'Inspect.', integrations=['github_api'], integration_request=payload)


@pytest.mark.parametrize('allowed', [[], None])
def test_project_must_allow_integration(controller, tmp_path, allowed):
    spec = manifest(tmp_path)
    if allowed is not None:
        spec['allowed_integrations'] = allowed
    controller.register_project(spec)
    with pytest.raises(ControllerError):
        controller.build_project_task('sample-project', 'Inspect.', integrations=['github_api'], integration_request={'verb': 'pr_get', 'params': {}})


@pytest.mark.parametrize('patch', [
    {'integrations': None}, {'integrations': 'github_api'}, {'integrations': [[]]},
    {'integrations': ['unknown']}, {'integrations': ['github_api', 'github_api']},
    {'integrations': ['github_api']}, {'integration_request': None},
    {'integration_request': {'verb': 'pr_get', 'params': {}}},
    {'required_capabilities': ['codex', 'github_api']},
    {'integrations': ['github_api'], 'integration_request': {'verb': 'pr_get', 'params': {}}},
    {'integrations': ['github_api'], 'integration_request': {'verb': 'merge', 'params': {}}, 'required_capabilities': ['codex', 'github_api']},
])
def test_enqueue_rejects_invalid_integration_snapshot(controller, patch):
    task = controller.build_project_task('sample-project', 'Inspect.')
    task.update(patch)
    with pytest.raises(ControllerError):
        controller.enqueue(task)


@pytest.mark.parametrize('command', ['build', 'create'])
def test_cli_opt_in(controller, command):
    def run(*extra):
        return subprocess.run([sys.executable, '-m', 'hermes_controller', '--runtime-root', str(controller.root), 'task', command, 'sample-project', '--instruction', 'Inspect.', *extra], cwd=Path(__file__).parents[1], capture_output=True, text=True)

    def snapshot(result):
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        if command == 'create':
            return json.loads(controller.db.execute('SELECT task_json FROM jobs WHERE job_id=?', (data['job_id'],)).fetchone()[0])
        return data

    normal = snapshot(run())
    assert 'github_api' not in normal['required_capabilities']
    request = {'verb': 'pr_get', 'params': {}}
    task = snapshot(run('--integration', 'github_api', '--integration-request', json.dumps(request)))
    assert task['integrations'] == ['github_api']
    assert task['integration_request'] == request
    assert 'github_api' in task['required_capabilities']
    assert run('--integration', 'github_api').returncode != 0
    assert run('--integration', 'github_api', '--integration', 'github_api', '--integration-request', json.dumps(request)).returncode != 0
    for verb in ('merge', 'push', 'unknown'):
        assert run('--integration', 'github_api', '--integration-request', json.dumps({'verb': verb, 'params': {}})).returncode != 0


def test_request_validator():
    from hermes_controller.github_api import GITHUB_API_VERBS, KNOWN_INTEGRATIONS, validate_github_api_request
    assert KNOWN_INTEGRATIONS == frozenset({'github_api'})
    assert GITHUB_API_VERBS == frozenset({'pr_create', 'pr_get', 'pr_list', 'actions_status', 'checks_status'})
    assert validate_github_api_request({'verb': 'pr_get', 'params': {}}) is None
    for request in (None, [], {}, {'verb': [], 'params': {}}, {'verb': 'push', 'params': {}}, {'verb': 'pr_get', 'params': None}):
        with pytest.raises(ValueError):
            validate_github_api_request(request)


@pytest.mark.parametrize("key", ["owner", "repo"])
def test_cannot_override_repository(controller, key):
    with pytest.raises(ControllerError, match="owner|repo"):
        controller.build_project_task("sample-project", "Inspect.", integrations=["github_api"],
                                      integration_request={"verb": "pr_get", "params": {key: "other"}})


def test_canonical_target_and_no_engine(controller, monkeypatch):
    import hermes_controller.controller as module
    def forbidden(*args, **kwargs):
        pytest.fail("integration selected an engine")
    monkeypatch.setattr(module, "select_engine", forbidden)
    task = controller.build_project_task("sample-project", "Inspect.", engine="auto",
        integrations=["github_api"], integration_request={"verb": "pr_get", "params": {}})
    assert task["github_repository"] == {"owner": "o", "repo": "r"}
    assert "execution_engine" not in task
    assert not set(task["required_capabilities"]) & {"codex", "claude", "hybrid"}
    task["github_repository"]["repo"] = "other"
    with pytest.raises(ControllerError):
        controller.enqueue(task)


@pytest.mark.parametrize("repository, expected", [
    ("git@github.com:Owner/repo.git", ("Owner", "repo")),
    ("https://github.com/Owner/repo.git", ("Owner", "repo")),
    ("https://github.com/Owner/repo", ("Owner", "repo")),
    ("http://github.com/o/r", None), ("https://evil.com/o/r", None),
    ("https://github.com/o/r?x=1", None), ("git@github.com:o/../r.git", None),
    ("https://github.com/o/r/extra", None), ("", None), (None, None),
])
def test_parse_repository(repository, expected):
    from hermes_controller.github_api import parse_github_repository
    if expected is None:
        with pytest.raises(ValueError):
            parse_github_repository(repository)
    else:
        assert parse_github_repository(repository) == expected


@pytest.mark.parametrize("change", ["repository", "allowlist"])
def test_enqueue_binds_registered_project(controller, tmp_path, change):
    task = controller.build_project_task("sample-project", "Inspect.",
        integrations=["github_api"], integration_request={"verb": "pr_get", "params": {}})
    if change == "repository":
        task["repository"] = "https://github.com/other/project"
        task["github_repository"] = {"owner": "other", "repo": "project"}
    else:
        controller.register_project(manifest(tmp_path, repository="git@github.com:o/r.git"))
    with pytest.raises(ControllerError):
        controller.enqueue(task)


@pytest.mark.parametrize("key", ["owner", "repo"])
def test_validator_rejects_target_override(key):
    from hermes_controller.github_api import validate_github_api_request
    with pytest.raises(ValueError, match="owner/repo"):
        validate_github_api_request({"verb": "pr_get", "params": {key: "other"}})
