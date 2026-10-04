"""Allow-listed GitHub API request contract, independent of execution engines."""

import json
import re
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .adapters import AdapterResult

KNOWN_INTEGRATIONS = frozenset({"github_api"})
GITHUB_API_VERBS = frozenset({
    "pr_create", "pr_get", "pr_list", "actions_status", "checks_status",
})


def validate_github_api_request(request) -> None:
    if not isinstance(request, dict):
        raise ValueError("github_api integration_request must be a dict")
    verb = request.get("verb")
    if not isinstance(verb, str) or verb not in GITHUB_API_VERBS:
        raise ValueError("unknown github_api verb")
    if not isinstance(request.get("params"), dict):
        raise ValueError("github_api params must be a dict")


# The integration deliberately has no generic HTTP or arbitrary-path entrypoint.

class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHubApiClient:
    """Bounded GitHub REST surface. Credentials stay in this client only.

    List methods return one explicit page (100 items by default); callers may
    supply page/per_page. Status methods use ref as a commit SHA.
    """

    def __init__(self, token, *, opener=None, timeout_s=15.0):
        self._token = token
        self._opener = opener or build_opener(_NoRedirect()).open
        self.timeout_s = timeout_s

    def redact(self, value):
        if isinstance(value, str):
            return value.replace(self._token, "[REDACTED]") if self._token else value
        if isinstance(value, dict):
            return {self.redact(key): self.redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        return value

    @staticmethod
    def _repo(owner, repo):
        for item in (owner, repo):
            if not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", item):
                raise ValueError("invalid repository")
        return f"/repos/{owner}/{repo}"

    @staticmethod
    def _positive(value, maximum=None):
        if type(value) is not int or value < 1 or (maximum and value > maximum):
            raise ValueError("invalid positive integer")
        return value

    @staticmethod
    def _text(value):
        if not isinstance(value, str) or not value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("invalid text")
        return value

    def _request(self, method, path, payload=None):
        if not isinstance(self._token, str) or not self._token.strip():
            raise ValueError("GitHub token is not configured")
        req = Request(
            "https://api.github.com" + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            method=method,
            headers={"Authorization": f"Bearer {self._token}", "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28",
                     "User-Agent": "Hermes-github-api"},
        )
        with self._opener(req, timeout=self.timeout_s) as response:
            data = json.loads(response.read())
        if not isinstance(data, (dict, list)):
            raise RuntimeError("invalid GitHub response")
        return self.redact(data)

    def pr_create(self, owner, repo, title, head, base, body="", draft=False):
        path = self._repo(owner, repo) + "/pulls"
        if not isinstance(body, str) or type(draft) is not bool:
            raise ValueError("invalid PR body or draft")
        payload = {"title": self._text(title), "head": self._text(head), "base": self._text(base), "body": body}
        if draft:
            payload["draft"] = True
        return self._request("POST", path, payload)

    def pr_get(self, owner, repo, number):
        return self._request("GET", self._repo(owner, repo) + f"/pulls/{self._positive(number)}")

    def pr_list(self, owner, repo, state="open", per_page=100, page=1):
        if state not in ("open", "closed", "all"):
            raise ValueError("invalid PR state")
        query = urlencode({"state": state, "per_page": self._positive(per_page, 100), "page": self._positive(page)})
        data = self._request("GET", self._repo(owner, repo) + "/pulls?" + query)
        if not isinstance(data, list):
            raise RuntimeError("invalid PR list response")
        return {"pull_requests": data, "page": page, "per_page": per_page}

    def actions_status(self, owner, repo, ref, per_page=100, page=1):
        query = urlencode({"head_sha": self._text(ref), "per_page": self._positive(per_page, 100), "page": self._positive(page)})
        return self._request("GET", self._repo(owner, repo) + "/actions/runs?" + query)

    def checks_status(self, owner, repo, ref, per_page=100, page=1):
        encoded = quote(self._text(ref), safe="")
        if encoded in (".", ".."):
            raise ValueError("invalid ref")
        query = urlencode({"per_page": self._positive(per_page, 100), "page": self._positive(page)})
        return self._request("GET", self._repo(owner, repo) + f"/commits/{encoded}/check-runs?" + query)


class GitHubApiHandler:
    """Allow-listed dispatch returning JSON evidence within the Hermes contract."""

    def __init__(self, client):
        self.client = client

    def execute(self, request) -> AdapterResult:
        verb = request.get("verb") if isinstance(request, dict) else None
        if not isinstance(verb, str) or verb not in GITHUB_API_VERBS:
            label = verb if isinstance(verb, str) else "<invalid>"
            if isinstance(self.client, GitHubApiClient):
                label = self.client.redact(label)
            return AdapterResult(status="BLOCKED", summary=f"github_api: unknown or forbidden verb {label}")
        try:
            validate_github_api_request(request)
            data = getattr(self.client, verb)(**request["params"])
            if not isinstance(data, dict):
                raise RuntimeError("invalid client result")
            if isinstance(self.client, GitHubApiClient):
                data = self.client.redact(data)
            evidence = json.dumps({"verb": verb, "result": data}, sort_keys=True)
        except (ValueError, TypeError):
            return AdapterResult(status="BLOCKED", summary="github_api: invalid request or missing configuration")
        except Exception:
            # Never serialize upstream errors: they may contain credentials or
            # request bodies. No automatic retries, especially for PR creation.
            return AdapterResult(status="FAILED", summary="github_api: request failed")
        return AdapterResult(status="DONE", summary=f"github_api: {verb} completed",
                             completed=[verb], evidence=[evidence])
