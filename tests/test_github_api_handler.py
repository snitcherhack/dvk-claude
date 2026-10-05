import json
from urllib.error import URLError

import pytest

from hermes_controller.github_api import GitHubApiClient, GitHubApiHandler, GITHUB_API_VERBS


class FakeClient:
    def __init__(self):
        self.calls = []

    def __getattr__(self, verb):
        assert verb in GITHUB_API_VERBS
        def call(**params):
            self.calls.append((verb, params))
            return {"number": 42, "html_url": "https://github.com/o/r/pull/42", "status": "completed"}
        return call


@pytest.mark.parametrize("verb", sorted(GITHUB_API_VERBS - {"pr_create"}))
def test_dispatch(verb):
    client = FakeClient()
    result = execute(client, {"verb": verb, "params": {}})
    assert result.status == "DONE"
    assert client.calls == [(verb, {"owner": "o", "repo": "r"})]
    assert json.loads(result.evidence[0]) == {"verb": verb, "result": {"number": 42, "html_url": "https://github.com/o/r/pull/42", "status": "completed"}}
    assert result.completed


@pytest.mark.parametrize("verb", ["unknown", "merge", "push"])
def test_forbidden(verb):
    client = FakeClient()
    result = execute(client, {"verb": verb, "params": {}})
    assert result.status == "BLOCKED"
    assert result.summary == f"github_api: unknown or forbidden verb {verb}"
    assert not client.calls


@pytest.mark.parametrize("payload", [None, {}, {"verb": "pr_get", "params": []}])
def test_malformed_request(payload):
    assert execute(FakeClient(), payload).status == "BLOCKED"


class Response:
    def __init__(self, payload):
        self.payload = payload
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self):
        return json.dumps(self.payload).encode()


@pytest.mark.parametrize("verb, params, method, suffix, payload", [
    ("pr_get", {"number": 42}, "GET", "/pulls/42", {"number": 42}),
    ("pr_list", {}, "GET", "/pulls?state=open&per_page=100&page=1", [{"number": 42}]),
    ("actions_status", {"ref": "feature/x"}, "GET", "/actions/runs?head_sha=feature%2Fx&per_page=100&page=1", {"workflow_runs": []}),
    ("checks_status", {"ref": "feature/x"}, "GET", "/commits/feature%2Fx/check-runs?per_page=100&page=1", {"check_runs": []}),
])
def test_http_contract(verb, params, method, suffix, payload):
    calls = []
    def opener(request, timeout):
        calls.append(request)
        assert timeout > 0
        assert request.get_header("Authorization") == "Bearer fake-canary"
        return Response(payload)
    client = GitHubApiClient("fake-canary", opener=opener)
    result = execute(client, {"verb": verb, "params": params})
    assert result.status == "DONE"
    assert calls[0].full_url == "https://api.github.com/repos/o/r" + suffix
    assert calls[0].method == method
    if method == "POST":
        assert json.loads(calls[0].data) == params
    assert isinstance(json.loads(result.evidence[0])["result"], dict)


@pytest.mark.parametrize("fail", [False, True])
def test_token_never_in_result(fail):
    token = "fake-secret-canary"
    def opener(request, timeout):
        if fail:
            raise URLError(token)
        return Response({"title": token, token: [token]})
    client = GitHubApiClient(token, opener=opener)
    result = execute(client, {"verb": "pr_get", "params": {"number": 1}})
    assert result.status == ("FAILED" if fail else "DONE")
    assert token not in json.dumps(result.result())
    result = execute(client, {"verb": token, "params": {}})
    assert token not in json.dumps(result.result())


def test_missing_token_blocks_without_http():
    def opener(*args, **kwargs):
        pytest.fail("no HTTP without token")
    result = execute(GitHubApiClient(None, opener=opener), {"verb": "pr_get", "params": {"number": 1}})
    assert result.status == "BLOCKED"


@pytest.mark.parametrize("params", [{"owner": "../evil", "repo": "r", "number": 1}, {"owner": "o", "repo": "r", "number": True}])
def test_invalid_parameters_do_not_send(params):
    def opener(*args, **kwargs):
        pytest.fail("invalid request sent")
    result = execute(GitHubApiClient("fake", opener=opener), {"verb": "pr_get", "params": params})
    assert result.status == "BLOCKED"


def execute(client, request):
    return GitHubApiHandler(client).execute({
        "repository": "git@github.com:o/r.git", "integration_request": request})


def pr(state="open"):
    return {"number": 42, "html_url": "https://github.com/o/r/pull/42",
            "state": state, "head": {"label": "o:feature", "ref": "feature"},
            "base": {"ref": "main"}}


@pytest.mark.parametrize("existing, post_fails, status, posts", [
    ([pr()], False, "DONE", 0), ([], False, "DONE", 1),
    ([pr(), pr()], False, "BLOCKED", 0), ([pr("closed")], False, "BLOCKED", 0),
    ([pr("unknown")], False, "BLOCKED", 0),
    ([], True, "DONE", 1),
])
def test_create_reconciliation(existing, post_fails, status, posts):
    calls = []
    def opener(request, timeout):
        calls.append(request)
        if request.method == "POST":
            if post_fails:
                raise URLError("fake-secret")
            return Response(pr())
        assert "head=o%3Afeature" in request.full_url
        assert "base=main" in request.full_url
        return Response([pr()] if post_fails and any(c.method == "POST" for c in calls) else existing)
    result = execute(GitHubApiClient("fake-secret", opener=opener),
                     {"verb": "pr_create", "params": {"title": "Title", "head": "feature", "base": "main"}})
    assert result.status == status
    assert sum(c.method == "POST" for c in calls) == posts
    if status == "BLOCKED":
        assert "manual reconcile required" in result.summary
    else:
        data = json.loads(result.evidence[0])["result"]
        assert data["number"] == 42
        assert data.get("reconciled", False) == bool(existing or post_fails)
    assert "fake-secret" not in json.dumps(result.result())


@pytest.mark.parametrize("task", [
    {"integration_request": {"verb": "pr_get", "params": {}}},
    {"repository": "https://github.com/o/r", "github_repository": {"owner": "evil", "repo": "r"},
     "integration_request": {"verb": "pr_get", "params": {}}},
    {"repository": "https://github.com/o/r",
     "integration_request": {"verb": "pr_get", "params": {"owner": "evil"}}},
])
def test_missing_or_tampered_target(task):
    client = FakeClient()
    assert GitHubApiHandler(client).execute(task).status == "BLOCKED"
    assert not client.calls


@pytest.mark.parametrize("mode", ["get_error", "post_error", "malformed", "full_page"])
def test_uncertain_creation_never_retries_post(mode):
    calls = []
    def opener(request, timeout):
        calls.append(request.method)
        if mode == "get_error" or request.method == "POST":
            raise URLError("fake-secret")
        if mode == "malformed":
            return Response([{"number": 42}])
        return Response([pr()] * 100 if mode == "full_page" else [])
    result = execute(GitHubApiClient("fake-secret", opener=opener),
                     {"verb": "pr_create", "params": {"title": "Title", "head": "feature", "base": "main"}})
    assert result.status == "BLOCKED"
    assert "manual reconcile required" in result.summary
    assert calls.count("POST") == (1 if mode == "post_error" else 0)
    assert "fake-secret" not in json.dumps(result.result())
