from hermes_controller.engine_policy import select_engine


def test_balanced_policy_routes_bugfix_to_codex():
    decision = select_engine("Corrige el fallo del test unitario.", ["codex", "claude", "hybrid"])
    assert decision.engine == "codex"
    assert decision.rule == "codex_implementation_or_bugfix"


def test_balanced_policy_routes_architecture_to_claude():
    decision = select_engine("Analiza la arquitectura y diseña una estrategia.", ["codex", "claude", "hybrid"])
    assert decision.engine == "claude"
    assert decision.rule == "claude_analysis_or_architecture"


def test_balanced_policy_routes_review_and_high_impact_to_hybrid():
    review = select_engine("Revisa este cambio antes de merge.", ["codex", "claude", "hybrid"])
    security = select_engine("Corrige un problema de seguridad en producción.", ["codex", "claude", "hybrid"])
    assert review.engine == "hybrid"
    assert security.engine == "hybrid"
    assert review.rule == "hybrid_review_or_high_impact"


def test_balanced_policy_uses_codex_as_neutral_default():
    decision = select_engine("Haz la tarea indicada.", ["codex", "claude", "hybrid"])
    assert decision.engine == "codex"
    assert decision.rule == "codex_balanced_default"


def test_balanced_policy_falls_back_when_hybrid_is_not_allowed():
    decision = select_engine("Revisión de seguridad crítica.", ["codex", "claude"])
    assert decision.engine == "claude"
    assert decision.rule == "hybrid_unavailable_fallback_claude"


def test_balanced_policy_single_allowed_engine_is_deterministic():
    decision = select_engine("Analiza la arquitectura.", ["codex"])
    assert decision.engine == "codex"
    assert decision.rule == "only_allowed_engine"
