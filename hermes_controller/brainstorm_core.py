"""Deterministic Brainstorm core: stage validation, anonymization, ranking and report.

Pure functions only: no filesystem, process, network, Controller, worker or
engine access. Every piece of model-produced text is opaque data: it is
validated for type and size and copied, never interpreted, and no decision
branches on its content.

Scores are exact ``fractions.Fraction`` values. They are serialized with
``format_number``: an exact decimal string when the value terminates in base
10, otherwise ``"numerator/denominator"``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from .brainstorm_contract import (
    MAX_CANDIDATE_COUNT,
    MIN_CANDIDATE_COUNT,
    validate_brainstorm_config,
    validate_rubric,
)

AUTHORS = ("claude", "codex")
MAX_SCORE = 10
CANDIDATE_ID = re.compile(r"C[0-9]{2}")

HIGH, MEDIUM, LOW = "HIGH", "MEDIUM", "LOW"
HIGH_MIN_SCORE, HIGH_MAX_DISAGREEMENT, HIGH_MIN_MARGIN = 75, 10, 5
MEDIUM_MIN_SCORE, MEDIUM_MAX_DISAGREEMENT = 65, 20
SELF_PREFERENCE_THRESHOLD = 10
DIVERGENCE_THRESHOLD = 10
CRITERION_DIVERGENCE_THRESHOLD = 3

RECOMMENDED_FOR_PILOT = "RECOMMENDED_FOR_PILOT"
INCONCLUSIVE = "INCONCLUSIVE"
VERDICTS = ("PASS", "FAIL")

# Text fields map to a maximum length; list fields to (max items, max chars per item).
PROPOSAL_TEXT = {"title": 120, "concept": 2000, "hook": 500, "audience_flow": 1500,
                 "execution_plan": 2000, "minimum_pilot": 1000}
PROPOSAL_LISTS = {"dependencies": (10, 300), "assumptions": (10, 300), "risks": (10, 300)}
PROPOSAL_FIELDS = ("title", "concept", "hook", "audience_flow", "execution_plan",
                   "dependencies", "assumptions", "risks", "minimum_pilot")
EVALUATION_LISTS = {"strengths": (10, 500), "weaknesses": (10, 500), "improvements": (10, 500),
                    "constraint_violations": (10, 500)}
REFINEMENT_TEXT = {"title": 120, "concept": 4000, "pilot_definition": 2000, "success_criterion": 1000}
REFINEMENT_LISTS = {"decisions_adopted": (10, 500), "discarded_elements": (10, 500), "accepted_risks": (10, 500)}
VALIDATION_LISTS = {"material_findings": (10, 1000)}


# --- serialization ---------------------------------------------------------------

def canonical_json(value: Any) -> str:
    """Byte-stable JSON: sorted keys, no whitespace, UTF-8 text, no NaN."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def format_number(value: Fraction | int | None) -> str | None:
    """Exact string for a score: terminating decimal or ``n/d``."""
    if value is None:
        return None
    number = Fraction(value)
    rest, twos, fives = number.denominator, 0, 0
    while rest % 2 == 0:
        rest, twos = rest // 2, twos + 1
    while rest % 5 == 0:
        rest, fives = rest // 5, fives + 1
    if rest != 1:
        return f"{number.numerator}/{number.denominator}"
    places = max(twos, fives)
    scaled = abs(number * 10 ** places)
    digits = str(scaled.numerator).rjust(places + 1, "0")
    text = f"{digits[:-places]}.{digits[-places:]}" if places else digits
    return f"-{text}" if number < 0 else text


# --- field validation --------------------------------------------------------------

def _exact_fields(value: Any, fields: tuple[str, ...] | list[str], where: str) -> None:
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError(f"{where} must have exactly the fields {sorted(fields)}")


def _text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be a non-empty string of at most {limit} characters")
    return value


def _text_list(value: Any, name: str, max_items: int, max_chars: int) -> list[str]:
    if not isinstance(value, list) or len(value) > max_items:
        raise ValueError(f"{name} must be a list of at most {max_items} items")
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item) > max_chars:
            raise ValueError(f"{name} items must be non-empty strings of at most {max_chars} characters")
    return list(value)


def _fields(value: dict[str, Any], texts: dict[str, int], lists: dict[str, tuple[int, int]]) -> dict[str, Any]:
    copied: dict[str, Any] = {name: _text(value[name], name, limit) for name, limit in texts.items()}
    copied.update({name: _text_list(value[name], name, *limits) for name, limits in lists.items()})
    return copied


def _candidate_id(value: Any) -> str:
    if not isinstance(value, str) or not CANDIDATE_ID.fullmatch(value):
        raise ValueError(f"invalid candidate_id: {value!r}")
    return value


def _rubric_ids(rubric: list[dict[str, Any]]) -> list[str]:
    validate_rubric(rubric)
    return [criterion["id"] for criterion in rubric]


def _scores(entries: Any, rubric: list[dict[str, Any]], where: str) -> dict[str, int]:
    criteria = _rubric_ids(rubric)
    if not isinstance(entries, list):
        raise ValueError(f"{where} scores must be a list")
    found: dict[str, int] = {}
    for entry in entries:
        _exact_fields(entry, ("criterion_id", "score"), f"{where} score entry")
        criterion_id, score = entry["criterion_id"], entry["score"]
        if not isinstance(criterion_id, str):
            raise ValueError(f"{where} criterion_id must be a string")
        if criterion_id in found:
            raise ValueError(f"{where} has a duplicate criterion: {criterion_id!r}")
        if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= MAX_SCORE:
            raise ValueError(f"{where} score must be an integer between 0 and {MAX_SCORE}")
        found[criterion_id] = score
    if set(found) != set(criteria):
        raise ValueError(f"{where} scores must cover exactly the rubric criteria {criteria}")
    return {criterion: found[criterion] for criterion in criteria}


def model_score(scores: dict[str, int], rubric: list[dict[str, Any]]) -> Fraction:
    """``sum(score_0_10 * weight) / 10`` as an exact value in 0..100."""
    return Fraction(sum(scores[criterion["id"]] * criterion["weight"] for criterion in rubric), 10)


# --- stage outputs -----------------------------------------------------------------

def validate_proposals(payload: Any, candidate_count: int) -> list[dict[str, Any]]:
    """Validate one engine's proposals stage; models never assign candidate IDs."""
    if not isinstance(candidate_count, int) or isinstance(candidate_count, bool) or not (
        MIN_CANDIDATE_COUNT <= candidate_count <= MAX_CANDIDATE_COUNT
    ):
        raise ValueError("invalid candidate_count")
    _exact_fields(payload, ("proposals",), "proposals stage output")
    proposals = payload["proposals"]
    if not isinstance(proposals, list) or len(proposals) != candidate_count:
        raise ValueError(f"proposals stage must return exactly {candidate_count} proposals")
    validated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in proposals:
        _exact_fields(item, PROPOSAL_FIELDS, "proposal")
        copied = _fields(item, PROPOSAL_TEXT, PROPOSAL_LISTS)
        key = canonical_json(copied)
        if key in seen:
            raise ValueError("proposals stage returned a duplicate proposal")
        seen.add(key)
        validated.append({name: copied[name] for name in PROPOSAL_FIELDS})
    return validated


def validate_evaluation(payload: Any, candidate_ids: list[str], rubric: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Validate one engine's evaluation of exactly the expected candidates."""
    expected = [_candidate_id(candidate_id) for candidate_id in candidate_ids]
    if len(set(expected)) != len(expected):
        raise ValueError("expected candidate IDs must be unique")
    _rubric_ids(rubric)
    _exact_fields(payload, ("evaluations",), "evaluation stage output")
    evaluations = payload["evaluations"]
    if not isinstance(evaluations, list):
        raise ValueError("evaluations must be a list")
    validated: dict[str, dict[str, Any]] = {}
    for item in evaluations:
        _exact_fields(item, ("candidate_id", "scores", *EVALUATION_LISTS), "evaluation")
        candidate_id = _candidate_id(item["candidate_id"])
        if candidate_id not in expected:
            raise ValueError(f"evaluation references an unknown candidate: {candidate_id}")
        if candidate_id in validated:
            raise ValueError(f"evaluation has a duplicate candidate: {candidate_id}")
        entry = {"scores": _scores(item["scores"], rubric, f"evaluation of {candidate_id}")}
        entry.update(_fields(item, {}, EVALUATION_LISTS))
        validated[candidate_id] = entry
    missing = sorted(set(expected) - set(validated))
    if missing:
        raise ValueError(f"evaluation is missing candidates: {missing}")
    return {candidate_id: validated[candidate_id] for candidate_id in sorted(validated)}


def validate_refinement(payload: Any, winner_id: str) -> dict[str, Any]:
    _exact_fields(payload, ("candidate_id", *REFINEMENT_TEXT, *REFINEMENT_LISTS), "refinement")
    if _candidate_id(payload["candidate_id"]) != winner_id:
        raise ValueError("refinement must reference the winner candidate")
    return {"candidate_id": winner_id, **_fields(payload, REFINEMENT_TEXT, REFINEMENT_LISTS)}


def validate_validation(payload: Any, winner_id: str, rubric: list[dict[str, Any]]) -> dict[str, Any]:
    _exact_fields(payload, ("candidate_id", "verdict", "scores", *VALIDATION_LISTS), "validation")
    if _candidate_id(payload["candidate_id"]) != winner_id:
        raise ValueError("validation must reference the winner candidate")
    if payload["verdict"] not in VERDICTS:
        raise ValueError("validation verdict must be PASS or FAIL")
    return {
        "candidate_id": winner_id,
        "verdict": payload["verdict"],
        "scores": _scores(payload["scores"], rubric, "validation"),
        **_fields(payload, {}, VALIDATION_LISTS),
    }


# --- anonymization -------------------------------------------------------------------

@dataclass(frozen=True)
class AnonymizedCandidates:
    bundle: tuple[dict[str, Any], ...]
    authors: dict[str, str]


def anonymize(job_id: str, claude: list[dict[str, Any]], codex: list[dict[str, Any]]) -> AnonymizedCandidates:
    """Assign opaque IDs in an order seeded by ``job_id`` and hide authorship.

    The order key is SHA-256 over the job, the author and the canonical JSON
    of the proposal, so it is reproducible and independent of list order,
    dict key order and Python's randomized ``hash``.
    """
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("job_id is required for anonymization")
    if not claude or not codex:
        raise ValueError("anonymization requires proposals from both engines")
    keyed: list[tuple[str, str, dict[str, Any]]] = []
    for author, proposals in (("claude", claude), ("codex", codex)):
        for proposal in proposals:
            _exact_fields(proposal, PROPOSAL_FIELDS, "proposal")
            copied = {name: proposal[name] for name in PROPOSAL_FIELDS}
            key = hashlib.sha256(canonical_json([job_id, author, copied]).encode("utf-8")).hexdigest()
            keyed.append((key, author, copied))
    if len({key for key, _, _ in keyed}) != len(keyed):
        raise ValueError("duplicate proposal within one engine")
    keyed.sort(key=lambda item: item[0])
    bundle, authors = [], {}
    for index, (_key, author, proposal) in enumerate(keyed, start=1):
        candidate_id = f"C{index:02d}"
        bundle.append({"candidate_id": candidate_id, **proposal})
        authors[candidate_id] = author
    return AnonymizedCandidates(bundle=tuple(bundle), authors=authors)


# --- ranking ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RankedCandidate:
    rank: int
    candidate_id: str
    author: str
    claude_score: Fraction
    codex_score: Fraction
    final_score: Fraction
    disagreement: Fraction


@dataclass(frozen=True)
class Ranking:
    entries: tuple[RankedCandidate, ...]
    winner: RankedCandidate | None
    runner_up: RankedCandidate | None
    margin: Fraction | None
    base_confidence: str
    confidence: str
    self_preference: dict[str, Fraction | None]
    self_preference_flag: bool


def _mean(values: list[Fraction]) -> Fraction:
    return sum(values, Fraction(0)) / len(values)


def _base_confidence(winner: RankedCandidate | None, margin: Fraction | None) -> str:
    if winner is None:
        return LOW
    if (winner.final_score >= HIGH_MIN_SCORE and winner.disagreement <= HIGH_MAX_DISAGREEMENT
            and margin is not None and margin >= HIGH_MIN_MARGIN):
        return HIGH
    if winner.final_score >= MEDIUM_MIN_SCORE and winner.disagreement <= MEDIUM_MAX_DISAGREEMENT:
        return MEDIUM
    return LOW


def rank(claude_evaluation: dict[str, dict[str, Any]], codex_evaluation: dict[str, dict[str, Any]],
         rubric: list[dict[str, Any]], authors: dict[str, str]) -> Ranking:
    """Deterministic ranking; self-preference only caps confidence, never the order."""
    _rubric_ids(rubric)
    if set(claude_evaluation) != set(codex_evaluation):
        raise ValueError("claude and codex evaluations must cover the same candidates")
    if set(authors) != set(claude_evaluation) or any(author not in AUTHORS for author in authors.values()):
        raise ValueError("authors must map every candidate to claude or codex")
    scores = {
        evaluator: {cid: model_score(evaluation[cid]["scores"], rubric) for cid in evaluation}
        for evaluator, evaluation in (("claude", claude_evaluation), ("codex", codex_evaluation))
    }
    unranked = []
    for candidate_id in authors:
        claude_score, codex_score = scores["claude"][candidate_id], scores["codex"][candidate_id]
        unranked.append((candidate_id, claude_score, codex_score,
                         (claude_score + codex_score) / 2, abs(claude_score - codex_score)))
    unranked.sort(key=lambda item: (-item[3], item[4], item[0]))
    entries = tuple(
        RankedCandidate(position, cid, authors[cid], claude_score, codex_score, final, disagreement)
        for position, (cid, claude_score, codex_score, final, disagreement) in enumerate(unranked, start=1)
    )
    self_preference: dict[str, Fraction | None] = {}
    for evaluator in AUTHORS:
        own = [score for cid, score in scores[evaluator].items() if authors[cid] == evaluator]
        other = [score for cid, score in scores[evaluator].items() if authors[cid] != evaluator]
        self_preference[evaluator] = _mean(own) - _mean(other) if own and other else None
    winner = entries[0] if entries else None
    runner_up = entries[1] if len(entries) > 1 else None
    margin = winner.final_score - runner_up.final_score if winner and runner_up else None
    base = _base_confidence(winner, margin)
    author_bias = self_preference.get(winner.author) if winner else None
    flag = author_bias is not None and author_bias >= SELF_PREFERENCE_THRESHOLD
    confidence = MEDIUM if flag and base == HIGH else base
    return Ranking(entries, winner, runner_up, margin, base, confidence, self_preference, flag)


def refiner_for(ranking: Ranking) -> str:
    """The author of the winner refines it."""
    if ranking.winner is None:
        raise ValueError("ranking has no winner to refine")
    return ranking.winner.author


def validator_for(ranking: Ranking) -> str:
    """The other engine validates the refined winner."""
    author = refiner_for(ranking)
    return next(engine for engine in AUTHORS if engine != author)


def decide(validation: dict[str, Any] | None) -> str:
    """PASS recommends the winner; FAIL (or no validation) is inconclusive. Never reselects."""
    if validation is None:
        return INCONCLUSIVE
    verdict = validation.get("verdict")
    if verdict == "PASS":
        return RECOMMENDED_FOR_PILOT
    if verdict == "FAIL":
        return INCONCLUSIVE
    raise ValueError("validation verdict must be PASS or FAIL")


# --- report ------------------------------------------------------------------------------

def _row(entry: RankedCandidate) -> dict[str, Any]:
    return {
        "rank": entry.rank,
        "candidate_id": entry.candidate_id,
        "author": entry.author,
        "claude_score": format_number(entry.claude_score),
        "codex_score": format_number(entry.codex_score),
        "final_score": format_number(entry.final_score),
        "disagreement": format_number(entry.disagreement),
    }


def build_report(*, job_id: str, question: str, config: dict[str, Any], anonymized: AnonymizedCandidates,
                 claude_evaluation: dict[str, dict[str, Any]], codex_evaluation: dict[str, dict[str, Any]],
                 ranking: Ranking, refinement: dict[str, Any] | None,
                 validation: dict[str, Any] | None) -> dict[str, Any]:
    """Assemble the JSON-serializable report; Markdown rendering belongs to the adapter."""
    validate_brainstorm_config(config)
    if not isinstance(question, str):
        raise ValueError("question must be a string")
    rubric = config["rubric"]
    candidate_ids = set(anonymized.authors)
    if {entry.candidate_id for entry in ranking.entries} != candidate_ids or set(claude_evaluation) != candidate_ids \
            or set(codex_evaluation) != candidate_ids:
        raise ValueError("report inputs must describe the same candidates")
    winner = ranking.winner
    for name, stage in (("refinement", refinement), ("validation", validation)):
        if stage is not None and (winner is None or stage.get("candidate_id") != winner.candidate_id):
            raise ValueError(f"{name} must reference the winner candidate")

    proposals = {item["candidate_id"]: item for item in anonymized.bundle}
    candidates = [
        {
            "candidate_id": cid,
            "author": anonymized.authors[cid],
            "proposal": {name: proposals[cid][name] for name in PROPOSAL_FIELDS},
            "claude_evaluation": claude_evaluation[cid],
            "codex_evaluation": codex_evaluation[cid],
        }
        for cid in sorted(candidate_ids)
    ]

    def headline(entry: RankedCandidate | None) -> dict[str, Any] | None:
        return None if entry is None else {**_row(entry), "title": proposals[entry.candidate_id]["title"]}

    risks: dict[str, Any] = {"winner_proposal": [], "claude_evaluation": {"weaknesses": [], "constraint_violations": []},
                             "codex_evaluation": {"weaknesses": [], "constraint_violations": []},
                             "refinement_accepted_risks": [], "validation_findings": []}
    winner_criteria: list[dict[str, Any]] = []
    if winner is not None:
        cid = winner.candidate_id
        risks["winner_proposal"] = list(proposals[cid]["risks"])
        for evaluator, evaluation in (("claude", claude_evaluation), ("codex", codex_evaluation)):
            risks[f"{evaluator}_evaluation"] = {key: list(evaluation[cid][key]) for key in ("weaknesses", "constraint_violations")}
        risks["refinement_accepted_risks"] = list(refinement["accepted_risks"]) if refinement else []
        risks["validation_findings"] = list(validation["material_findings"]) if validation else []
        for criterion in rubric:
            claude_value = claude_evaluation[cid]["scores"][criterion["id"]]
            codex_value = codex_evaluation[cid]["scores"][criterion["id"]]
            if abs(claude_value - codex_value) >= CRITERION_DIVERGENCE_THRESHOLD:
                winner_criteria.append({"criterion_id": criterion["id"], "claude": claude_value,
                                        "codex": codex_value, "difference": abs(claude_value - codex_value)})

    divergent = sorted((entry for entry in ranking.entries if entry.disagreement > DIVERGENCE_THRESHOLD),
                       key=lambda entry: (-entry.disagreement, entry.candidate_id))
    validation_report = None
    if validation is not None:
        validation_report = {**validation, "model_score": format_number(model_score(validation["scores"], rubric))}

    return {
        "version": config["version"],
        "job_id": job_id,
        "question": question,
        "candidate_count": config["candidate_count"],
        "rubric_id": config["rubric_id"],
        "rubric": [dict(criterion) for criterion in rubric],
        "method": {
            "model_score": "sum(score_0_10 * weight) / 10",
            "final_score": "(claude_score + codex_score) / 2",
            "disagreement": "abs(claude_score - codex_score)",
            "tie_break": ["higher final_score", "lower disagreement", "lower candidate_id"],
            "numbers": "exact; terminating decimal or numerator/denominator",
        },
        "candidates": candidates,
        "ranking": [_row(entry) for entry in ranking.entries],
        "winner": headline(winner),
        "runner_up": headline(ranking.runner_up),
        "margin": format_number(ranking.margin),
        "base_confidence": ranking.base_confidence,
        "confidence": ranking.confidence,
        "self_preference": {engine: format_number(ranking.self_preference.get(engine)) for engine in AUTHORS},
        "self_preference_threshold": SELF_PREFERENCE_THRESHOLD,
        "self_preference_flag": ranking.self_preference_flag,
        "refinement": refinement,
        "validation": validation_report,
        "decision_status": decide(validation),
        "risks": risks,
        "divergences": {
            "candidate_threshold": DIVERGENCE_THRESHOLD,
            "candidates": [{"candidate_id": entry.candidate_id, "disagreement": format_number(entry.disagreement)}
                           for entry in divergent],
            "criterion_threshold": CRITERION_DIVERGENCE_THRESHOLD,
            "winner_criteria": winner_criteria,
        },
    }


# --- stage schemas (structural only; semantics are enforced by the validators) ------------

def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


_STRING = {"type": "string"}
_STRINGS = {"type": "array", "items": {"type": "string"}}
_SCORES = {"type": "array", "items": _object({"criterion_id": _STRING, "score": {"type": "integer"}})}


def _field_schema(texts: dict[str, int], lists: dict[str, tuple[int, int]], order: tuple[str, ...]) -> dict[str, Any]:
    return {name: (_STRING if name in texts else _STRINGS) for name in order}


STAGE_SCHEMAS: dict[str, dict[str, Any]] = {
    "proposals": _object({
        "proposals": {"type": "array", "items": _object(_field_schema(PROPOSAL_TEXT, PROPOSAL_LISTS, PROPOSAL_FIELDS))},
    }),
    "evaluation": _object({
        "evaluations": {"type": "array", "items": _object({
            "candidate_id": _STRING, "scores": _SCORES, **{name: _STRINGS for name in EVALUATION_LISTS},
        })},
    }),
    "refinement": _object({
        "candidate_id": _STRING,
        **_field_schema(REFINEMENT_TEXT, REFINEMENT_LISTS, (*REFINEMENT_TEXT, *REFINEMENT_LISTS)),
    }),
    "validation": _object({
        "candidate_id": _STRING,
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "material_findings": _STRINGS,
        "scores": _SCORES,
    }),
}
