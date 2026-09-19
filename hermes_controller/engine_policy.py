"""Deterministic engine-selection policies for Hermes project tasks."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class EngineDecision:
    engine: str
    policy: str
    rule: str


_BALANCED_POLICY = "balanced-v1"

_HYBRID_PHRASES = (
    "review",
    "code review",
    "cross check",
    "cross-check",
    "double check",
    "verify",
    "validate",
    "security",
    "secure",
    "sensitive",
    "critical",
    "production",
    "prod",
    "migration",
    "migrate",
    "breaking change",
    "large refactor",
    "major refactor",
    "revisa",
    "revision",
    "verifica",
    "validacion",
    "seguridad",
    "sensible",
    "critico",
    "produccion",
    "migracion",
    "refactor grande",
    "refactorizacion grande",
)

_CLAUDE_PHRASES = (
    "architecture",
    "architect",
    "design",
    "analysis",
    "analyze",
    "investigate",
    "research",
    "plan",
    "strategy",
    "tradeoff",
    "trade off",
    "root cause",
    "document",
    "documentation",
    "arquitectura",
    "diseno",
    "analisis",
    "analiza",
    "investiga",
    "investigacion",
    "planifica",
    "planificacion",
    "estrategia",
    "causa raiz",
    "documenta",
    "documentacion",
)

_CODEX_PHRASES = (
    "implement",
    "fix",
    "bug",
    "error",
    "failing test",
    "test failure",
    "unit test",
    "integration test",
    "add endpoint",
    "add feature",
    "refactor",
    "code",
    "implementa",
    "corrige",
    "fallo",
    "prueba",
    "test",
    "anade",
    "agrega",
    "codigo",
    "refactoriza",
)


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_text = "".join(char for char in decomposed if not unicodedata.combining(char))
    lowered = ascii_text.lower().replace("_", " ")
    return re.sub(r"[^a-z0-9]+", " ", lowered).strip()


def _contains_phrase(text: str, phrases: Iterable[str]) -> bool:
    padded = f" {text} "
    for phrase in phrases:
        normalized = _normalize(phrase)
        if f" {normalized} " in padded:
            return True
    return False


def select_engine(
    instruction: str,
    allowed_engines: list[str],
    *,
    policy: str = _BALANCED_POLICY,
) -> EngineDecision:
    """Select an execution engine deterministically for auto mode."""
    if policy != _BALANCED_POLICY:
        raise ValueError(f"unsupported engine policy: {policy}")
    allowed = list(dict.fromkeys(allowed_engines))
    if not allowed:
        raise ValueError("allowed_engines must not be empty")
    if len(allowed) == 1:
        return EngineDecision(allowed[0], policy, "only_allowed_engine")

    text = _normalize(instruction)

    if _contains_phrase(text, _HYBRID_PHRASES):
        if "hybrid" in allowed:
            return EngineDecision("hybrid", policy, "hybrid_review_or_high_impact")
        if "claude" in allowed:
            return EngineDecision("claude", policy, "hybrid_unavailable_fallback_claude")
        if "codex" in allowed:
            return EngineDecision("codex", policy, "hybrid_unavailable_fallback_codex")

    if _contains_phrase(text, _CLAUDE_PHRASES):
        if "claude" in allowed:
            return EngineDecision("claude", policy, "claude_analysis_or_architecture")
        if "codex" in allowed:
            return EngineDecision("codex", policy, "claude_unavailable_fallback_codex")
        if "hybrid" in allowed:
            return EngineDecision("hybrid", policy, "claude_unavailable_fallback_hybrid")

    if _contains_phrase(text, _CODEX_PHRASES):
        if "codex" in allowed:
            return EngineDecision("codex", policy, "codex_implementation_or_bugfix")
        if "claude" in allowed:
            return EngineDecision("claude", policy, "codex_unavailable_fallback_claude")
        if "hybrid" in allowed:
            return EngineDecision("hybrid", policy, "codex_unavailable_fallback_hybrid")

    for engine, rule in (
        ("codex", "codex_balanced_default"),
        ("claude", "claude_balanced_default"),
        ("hybrid", "hybrid_balanced_default"),
        ("native", "native_balanced_default"),
    ):
        if engine in allowed:
            return EngineDecision(engine, policy, rule)
    raise ValueError("no supported engine is allowed")
