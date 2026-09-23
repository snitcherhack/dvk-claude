"""Task-level contract for the explicit-only Hermes Brainstorm engine."""

from __future__ import annotations

import copy
import re
from typing import Any

BRAINSTORM_VERSION = "brainstorm-v1"
DEFAULT_CANDIDATE_COUNT = 3
MIN_CANDIDATE_COUNT = 2
MAX_CANDIDATE_COUNT = 6
MIN_CRITERIA = 3
MAX_CRITERIA = 10
MAX_LABEL_CHARS = 80
MAX_DESCRIPTION_CHARS = 500

GENERAL_V1_RUBRIC_ID = "general-v1"
CUSTOM_RUBRIC_ID = "custom"
GENERAL_V1_RUBRIC: tuple[dict[str, Any], ...] = (
    {"id": "value", "label": "Value", "description": "Expected value for the stated goal.", "weight": 25},
    {"id": "originality", "label": "Originality", "description": "Useful differentiation and novelty.", "weight": 15},
    {"id": "project_fit", "label": "Project fit", "description": "Fit with project, audience and constraints.", "weight": 15},
    {"id": "feasibility", "label": "Feasibility", "description": "Technical and operational feasibility.", "weight": 20},
    {"id": "repeatability", "label": "Repeatability", "description": "Potential to repeat or scale.", "weight": 10},
    {"id": "evidence", "label": "Evidence", "description": "Availability of data, sources and assets.", "weight": 10},
    {"id": "pilotability", "label": "Pilotability", "description": "Ease of validating with a small pilot.", "weight": 5},
)

_CONFIG_FIELDS = {"version", "candidate_count", "rubric_id", "rubric"}
_CRITERION_FIELDS = {"id", "label", "description", "weight"}
_CRITERION_ID = re.compile(r"[a-z][a-z0-9_]{0,31}")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_candidate_count(value: Any) -> None:
    if not _is_int(value) or not MIN_CANDIDATE_COUNT <= value <= MAX_CANDIDATE_COUNT:
        raise ValueError(
            f"brainstorm candidate_count must be an integer between {MIN_CANDIDATE_COUNT} and {MAX_CANDIDATE_COUNT}"
        )


def _validate_rubric(rubric: Any) -> None:
    if not isinstance(rubric, list) or not MIN_CRITERIA <= len(rubric) <= MAX_CRITERIA:
        raise ValueError(f"brainstorm rubric must contain between {MIN_CRITERIA} and {MAX_CRITERIA} criteria")
    seen: set[str] = set()
    for criterion in rubric:
        if not isinstance(criterion, dict) or set(criterion) != _CRITERION_FIELDS:
            raise ValueError("brainstorm rubric criterion must have exactly the fields id, label, description, weight")
        criterion_id = criterion["id"]
        if not isinstance(criterion_id, str) or not _CRITERION_ID.fullmatch(criterion_id):
            raise ValueError("brainstorm rubric criterion id is invalid")
        if criterion_id in seen:
            raise ValueError("brainstorm rubric criterion ids must be unique")
        seen.add(criterion_id)
        label = criterion["label"]
        if not isinstance(label, str) or not label.strip() or len(label) > MAX_LABEL_CHARS:
            raise ValueError("brainstorm rubric criterion label is invalid")
        description = criterion["description"]
        if not isinstance(description, str) or not description.strip() or len(description) > MAX_DESCRIPTION_CHARS:
            raise ValueError("brainstorm rubric criterion description is invalid")
        weight = criterion["weight"]
        if not _is_int(weight) or not 1 <= weight <= 100:
            raise ValueError("brainstorm rubric criterion weight must be an integer between 1 and 100")
    if sum(criterion["weight"] for criterion in rubric) != 100:
        raise ValueError("brainstorm rubric weights must sum to exactly 100")


def validate_brainstorm_config(config: Any) -> None:
    """Validate a materialized brainstorm block; raise ValueError on any violation."""
    if not isinstance(config, dict) or set(config) != _CONFIG_FIELDS:
        raise ValueError("brainstorm config must have exactly the fields version, candidate_count, rubric_id, rubric")
    if config["version"] != BRAINSTORM_VERSION:
        raise ValueError(f"unsupported brainstorm version: {config['version']!r}")
    _validate_candidate_count(config["candidate_count"])
    _validate_rubric(config["rubric"])
    rubric_id = config["rubric_id"]
    if rubric_id == GENERAL_V1_RUBRIC_ID:
        if config["rubric"] != [dict(item) for item in GENERAL_V1_RUBRIC]:
            raise ValueError("brainstorm rubric does not match general-v1")
    elif rubric_id != CUSTOM_RUBRIC_ID:
        raise ValueError(f"unsupported brainstorm rubric_id: {rubric_id!r}")


def build_brainstorm_config(*, candidate_count: int | None = None, rubric: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Build the immutable brainstorm block embedded in a task snapshot.

    The rubric is always materialized so a queued task never depends on a
    later change to the default rubric.
    """
    count = DEFAULT_CANDIDATE_COUNT if candidate_count is None else candidate_count
    if rubric:
        rubric_id, criteria = CUSTOM_RUBRIC_ID, copy.deepcopy(rubric)
    else:
        rubric_id, criteria = GENERAL_V1_RUBRIC_ID, [dict(item) for item in GENERAL_V1_RUBRIC]
    config = {"version": BRAINSTORM_VERSION, "candidate_count": count, "rubric_id": rubric_id, "rubric": criteria}
    validate_brainstorm_config(config)
    return config
