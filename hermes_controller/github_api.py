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


EVIDENCE_ITEMS_LIMIT = 25


def _summarize_pull(data):
    return {
        **{key: data.get(key) for key in ("number", "state", "draft", "html_url")},
        "head": {key: data.get("head", {}).get(key) for key in ("ref", "sha")},
        "base": {"ref": data.get("base", {}).get("ref")},
    }


def _summarize_pr_list(data):
    pulls = data.get("pull_requests", [])
    # REST pulls supplies no total across pages: count all available rows in
    # this response, before evidence truncation. Explicit page/per_page remain
    # available for future queries; full REST responses are not transported.
    return {"count": len(pulls), "pulls": [
        {"number": row.get("number"), "url": row.get("html_url"),
         "state": row.get("state"), "head_ref": row.get("head", {}).get("ref"),
         "base_ref": row.get("base", {}).get("ref")}
        for row in pulls[:EVIDENCE_ITEMS_LIMIT]
    ]}


def _summarize_actions_status(data):
    runs = data.get("workflow_runs", [])
    return {"total_count": data.get("total_count", len(runs)), "runs": [
        {key: row.get(key) for key in (
            "id", "name", "head_sha", "status", "conclusion", "html_url", "created_at", "updated_at")}
        for row in runs[:EVIDENCE_ITEMS_LIMIT]
    ]}


def _summarize_checks_status(data):
    checks = data.get("check_runs", [])
    return {"total_count": data.get("total_count", len(checks)), "checks": [
        {key: row.get(key) for key in (
            "name", "status", "conclusion", "details_url", "started_at", "completed_at")}
        for row in checks[:EVIDENCE_ITEMS_LIMIT]
    ]}


def validate_github_api_request(request) -> None:
    if not isinstance(request, dict):
        raise ValueError("github_api integration_request must be a dict")
    verb = request.get("verb")
    if not isinstance(verb, str) or verb not in GITHUB_API_VERBS:
        raise ValueError("unknown github_api verb")
    if not isinstance(request.get("params"), dict):
        raise ValueError("github_api params must be a dict")

    if {"owner", "repo"} & request["params"].keys():
        raise ValueError("github_api params cannot override owner/repo")


def parse_github_repository(repository: str) -> tuple[str, str]:
    """Accept only canonical GitHub SSH/HTTPS repository URLs."""
    if not isinstance(repository, str):
        raise ValueError("invalid GitHub repository")
    match = re.fullmatch(
        r"(?:git@github\.com:|https://github\.com/)"
        r"([A-Za-z0-9_-][A-Za-z0-9_.-]*)/([A-Za-z0-9_-][A-Za-z0-9_.-]*)",
        repository,
    )
    if not match:
        raise ValueError("invalid GitHub repository")
    owner, repo = match.groups()
    repo = repo.removesuffix(".git")
    if not repo:
        raise ValueError("invalid GitHub repository")
    return owner, repo


def github_task_repository(task):
    owner, repo = parse_github_repository(task.get("repository"))
    if task.get("github_repository", {"owner": owner, "repo": repo}) != {"owner": owner, "repo": repo}:
        raise ValueError("github_repository does not match repository")
    return owner, repo


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

    def pr_list(self, owner, repo, state="open", per_page=100, page=1, head=None, base=None):
        if state not in ("open", "closed", "all"):
            raise ValueError("invalid PR state")
        query = urlencode({"state": state, "per_page": self._positive(per_page, 100), "page": self._positive(page)})
        if head is not None:
            query += "&" + urlencode({"head": self._text(head), "base": self._text(base)})
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

    def _reconcile(self, owner, repo, head, base):
        # head + base is the restart-stable idempotency key. Include closed PRs:
        # a closed/unknown match must never authorize another blind creation.
        head = GitHubApiClient._text(head)
        base = GitHubApiClient._text(base)
        label = head if ":" in head else f"{owner}:{head}"
        data = self.client.pr_list(owner=owner, repo=repo, state="all", head=label, base=base)
        rows = data["pull_requests"]
        if not isinstance(rows, list) or len(rows) >= 100:
            raise ValueError("incomplete reconciliation")
        matches = []
        for row in rows:
            if (not isinstance(row, dict) or row.get("head", {}).get("label") != label
                    or row.get("base", {}).get("ref") != base):
                raise ValueError("uncertain reconciliation")
            matches.append(row)
        if not matches:
            return None
        if len(matches) != 1 or matches[0].get("state") != "open":
            raise ValueError("ambiguous reconciliation")
        self._pr_identity(matches[0])
        return {**matches[0], "reconciled": True}

    @staticmethod
    def _pr_identity(data):
        if (not isinstance(data, dict) or type(data.get("number")) is not int
                or data["number"] < 1 or not isinstance(data.get("html_url"), str)
                or not data["html_url"].startswith("https://github.com/")):
            raise ValueError("missing PR identity")

    def pr_create(self, owner, repo, params):
        existing = self._reconcile(owner, repo, params.get("head"), params.get("base"))
        if existing is not None:
            return existing
        try:
            data = self.client.pr_create(owner=owner, repo=repo, **params)
            self._pr_identity(data)
            return data
        except Exception:
            # POST is issued at most once. A lost response may hide a successful
            # creation, so only GET reconciliation is safe here.
            existing = self._reconcile(owner, repo, params.get("head"), params.get("base"))
            if existing is None:
                raise ValueError("creation outcome unknown") from None
            return existing

    def execute(self, task) -> AdapterResult:
        request = task.get("integration_request") if isinstance(task, dict) else None
        verb = request.get("verb") if isinstance(request, dict) else None
        if not isinstance(verb, str) or verb not in GITHUB_API_VERBS:
            label = verb if isinstance(verb, str) else "<invalid>"
            if isinstance(self.client, GitHubApiClient):
                label = self.client.redact(label)
            return AdapterResult(status="BLOCKED", summary=f"github_api: unknown or forbidden verb {label}")
        try:
            validate_github_api_request(request)
            owner, repo = github_task_repository(task)
            if verb == "pr_create":
                try:
                    data = self.pr_create(owner, repo, request["params"])
                except Exception:
                    return AdapterResult(status="BLOCKED", summary="github_api: manual reconcile required")
            else:
                data = getattr(self.client, verb)(owner=owner, repo=repo, **request["params"])
            if not isinstance(data, dict):
                raise RuntimeError("invalid client result")
            if isinstance(self.client, GitHubApiClient):
                data = self.client.redact(data)
            if verb in ("pr_create", "pr_get"):
                summary = {"pull": _summarize_pull(data)}
            else:
                summary = {
                    "pr_list": _summarize_pr_list,
                    "actions_status": _summarize_actions_status,
                    "checks_status": _summarize_checks_status,
                }[verb](data)
            evidence = json.dumps({"verb": verb, **summary}, sort_keys=True)
        except (ValueError, TypeError):
            return AdapterResult(status="BLOCKED", summary="github_api: invalid request or missing configuration")
        except Exception:
            # Never serialize upstream errors: they may contain credentials or
            # request bodies. No automatic retries, especially for PR creation.
            return AdapterResult(status="FAILED", summary="github_api: request failed")
        return AdapterResult(status="DONE", summary=f"github_api: {verb} completed",
                             completed=[verb], evidence=[evidence])
