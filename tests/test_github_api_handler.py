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


@pytest.mark.parametrize("verb", sorted(GITHUB_API_VERBS))
def test_dispatch(verb):
    client = FakeClient()
    result = GitHubApiHandler(client).execute({"verb": verb, "params": {"owner": "o", "repo": "r"}})
    assert result.status == "DONE"
    assert client.calls == [(verb, {"owner": "o", "repo": "r"})]
    assert json.loads(result.evidence[0]) == {"verb": verb, "result": {"number": 42, "html_url": "https://github.com/o/r/pull/42", "status": "completed"}}
    assert result.completed


@pytest.mark.parametrize("verb", ["unknown", "merge", "push"])
def test_forbidden(verb):
    client = FakeClient()
    result = GitHubApiHandler(client).execute({"verb": verb, "params": {}})
    assert result.status == "BLOCKED"
    assert result.summary == f"github_api: unknown or forbidden verb {verb}"
    assert not client.calls


@pytest.mark.parametrize("payload", [None, {}, {"verb": "pr_get", "params": []}])
def test_malformed_request(payload):
    assert GitHubApiHandler(FakeClient()).execute(payload).status == "BLOCKED"


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
    ("pr_create", {"title": "Title", "head": "feature", "base": "main", "body": "Body"}, "POST", "/pulls", {"number": 42}),
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
    result = GitHubApiHandler(client).execute({"verb": verb, "params": {"owner": "o", "repo": "r", **params}})
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
    result = GitHubApiHandler(client).execute({"verb": "pr_get", "params": {"owner": "o", "repo": "r", "number": 1}})
    assert result.status == ("FAILED" if fail else "DONE")
    assert token not in json.dumps(result.result())
    result = GitHubApiHandler(client).execute({"verb": token, "params": {}})
    assert token not in json.dumps(result.result())


def test_missing_token_blocks_without_http():
    def opener(*args, **kwargs):
        pytest.fail("no HTTP without token")
    result = GitHubApiHandler(GitHubApiClient(None, opener=opener)).execute({"verb": "pr_get", "params": {"owner": "o", "repo": "r", "number": 1}})
    assert result.status == "BLOCKED"


@pytest.mark.parametrize("params", [{"owner": "../evil", "repo": "r", "number": 1}, {"owner": "o", "repo": "r", "number": True}])
def test_invalid_parameters_do_not_send(params):
    def opener(*args, **kwargs):
        pytest.fail("invalid request sent")
    result = GitHubApiHandler(GitHubApiClient("fake", opener=opener)).execute({"verb": "pr_get", "params": params})
    assert result.status == "BLOCKED"
