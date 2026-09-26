"""Load and validate the declarative alert rules.

Kept separate from :mod:`alerts` so the engine stays free of file formats, and
so a malformed config produces one clear error at startup rather than an
exception buried in a subscription task that nobody sees fail.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from .alerts import OPERATORS, Rule, RuleConfigError

#: Repository root when running from a source checkout; ``/app`` in the
#: container, where the config is mounted alongside the source.
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = Path(
    os.environ.get("ALERT_RULES_FILE", str(REPO_ROOT / "api" / "config" / "alerts.yaml"))
)


def _conditions(spec: dict[str, Any], rule_id: str) -> tuple:
    """Build the main condition plus any ``when:`` gates, as a single AND tuple.

    Merging them into one tuple is what lets the engine evaluate a rule with a
    single ``all(...)``, and keeps gating from becoming a second code path in the
    state machine.
    """
    from .alerts import Condition

    def make(entry: dict[str, Any], where: str) -> Condition:
        try:
            metric = entry["metric"]
            operator = entry["operator"]
            threshold = entry["threshold"]
        except KeyError as exc:
            raise RuleConfigError(f"rule {rule_id!r}: {where} is missing {exc.args[0]!r}") from exc
        if operator not in OPERATORS:
            raise RuleConfigError(
                f"rule {rule_id!r}: unknown operator {operator!r}, expected one of {OPERATORS}"
            )
        try:
            numeric = float(threshold)
        except (TypeError, ValueError) as exc:
            raise RuleConfigError(
                f"rule {rule_id!r}: threshold for {metric!r} is not numeric: {threshold!r}"
            ) from exc
        return Condition(metric=metric, operator=operator, threshold=numeric)

    conditions = [make(spec, "condition")]
    conditions.extend(make(entry, "when entry") for entry in spec.get("when") or [])
    return tuple(conditions)


def load_rules(path: Path | str | None = None) -> list[Rule]:
    """Read, validate and return the rule set.

    Raises :class:`RuleConfigError` on anything malformed. That is deliberate:
    a silently dropped rule is an alert that never fires, which is the failure
    mode most likely to go unnoticed.
    """
    target = Path(path) if path else DEFAULT_CONFIG
    if not target.exists():
        raise RuleConfigError(f"alert rules not found at {target}")

    document = yaml.safe_load(target.read_text()) or {}
    specs = document.get("rules")
    if not isinstance(specs, list) or not specs:
        raise RuleConfigError(f"{target}: no `rules:` list found")

    rules: list[Rule] = []
    for index, spec in enumerate(specs):
        if not isinstance(spec, dict):
            raise RuleConfigError(f"{target}: rules[{index}] is not a mapping")
        rule_id = spec.get("id")
        if not rule_id:
            raise RuleConfigError(f"{target}: rules[{index}] has no `id`")
        kind = spec.get("kind", "threshold")
        try:
            rules.append(
                Rule(
                    id=rule_id,
                    description=spec.get("description", rule_id),
                    scope=spec.get("scope", "inverter"),
                    severity=spec.get("severity", "warning"),
                    debounce_s=float(spec.get("debounce_s", 60.0)),
                    kind=kind,
                    stale_after_s=float(spec.get("stale_after_s", 0.0) or 0.0),
                    forget_after_s=float(spec.get("forget_after_s", 3600.0)),
                    # Staleness rules have no metrics to compare.
                    conditions=() if kind == "staleness" else _conditions(spec, rule_id),
                )
            )
        except (TypeError, ValueError) as exc:
            raise RuleConfigError(f"{target}: rule {rule_id!r} is invalid: {exc}") from exc
    return rules
