"""Allow-listed GitHub API request contract, independent of execution engines."""

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
