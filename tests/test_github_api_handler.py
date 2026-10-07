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
    assert json.loads(result.evidence[0])["verb"] == verb
    assert result.completed


@pytest.mark.parametrize("verb", ["unknown", "merge", "push", "checks_status"])
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
    ("commit_status", {"ref": "feature/x"}, "GET", "/commits/feature%2Fx/status?per_page=100&page=1", {"state": "pending", "sha": "abc", "statuses": []}),
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
    assert json.loads(result.evidence[0])["verb"] == verb


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
        data = json.loads(result.evidence[0])["pull"]
        assert data["number"] == 42
        assert "reconciled" not in data
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


@pytest.mark.parametrize("verb", sorted(GITHUB_API_VERBS))
def test_evidence_is_bounded_exact_deterministic_and_secret_free(verb):
    from hermes_controller import github_api

    token = "fake-evidence-secret"
    extra = {"body": "large" * 10000, "diff_url": "unused", "user": {"login": "unused"},
             "labels": list(range(1000)), "extra": list(range(1000)),
             "headers": {"Authorization": token}, "token": token}
    pull = {**pr(), "draft": False, **extra,
            "head": {"ref": "feature", "sha": "abc", "label": "o:feature", **extra},
            "base": {"ref": "main", **extra}}
    run = {key: key + "-value" for key in (
        "id", "name", "head_sha", "status", "conclusion", "html_url", "created_at", "updated_at")}
    status = {key: key + "-value" for key in (
        "context", "state", "description", "target_url", "created_at", "updated_at")}
    payload = {"pr_get": pull, "pr_create": pull, "pr_list": [pull] * 100,
               "actions_status": {"total_count": 100, "workflow_runs": [{**run, **extra}] * 100, **extra},
               "commit_status": {"state": "success", "sha": "abc", "total_count": 100,
                                 "statuses": [{**status, **extra}] * 100, **extra}}[verb]

    def opener(request, timeout):
        if verb == "pr_create" and request.method == "GET":
            return Response([])
        return Response(payload)

    params = {"pr_get": {"number": 42}, "pr_create": {"title": "Title", "head": "feature", "base": "main"},
              "pr_list": {}, "actions_status": {"ref": "abc"}, "commit_status": {"ref": "abc"}}[verb]
    client = GitHubApiClient(token, opener=opener)
    results = [execute(client, {"verb": verb, "params": params}) for _ in range(2)]
    assert all(result.status == "DONE" for result in results)
    data = json.loads(results[0].evidence[0])
    assert data == json.loads(results[1].evidence[0])
    serialized = json.dumps(results[0].evidence)
    for forbidden in (token, "Authorization", "headers", "diff_url", "labels", "large", "extra"):
        assert forbidden not in serialized
    if verb in ("pr_get", "pr_create"):
        assert data == {"verb": verb, "pull": {
            "number": 42, "state": "open", "draft": False,
            "html_url": pull["html_url"], "head": {"ref": "feature", "sha": "abc"},
            "base": {"ref": "main"}}}
    else:
        key, count_key, expected = {
            "pr_list": ("pulls", "count", {"number": 42, "url": pull["html_url"],
                                         "state": "open", "head_ref": "feature", "base_ref": "main"}),
            "actions_status": ("runs", "total_count", run),
        }.get(verb, (None, None, None))
        if verb == "commit_status":
            assert set(data) == {"verb", "state", "sha", "total_count", "statuses"}
            assert data["state"] == "success"
            assert data["sha"] == "abc"
            assert data["total_count"] == 100
            assert data["statuses"] == [status] * github_api.EVIDENCE_ITEMS_LIMIT
        else:
            assert set(data) == {"verb", key, count_key}
            assert data[count_key] == 100
            assert data[key] == [expected] * github_api.EVIDENCE_ITEMS_LIMIT
