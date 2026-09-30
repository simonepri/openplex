"""Synthesizes actionable review checklists and rubric criteria directly from matching RuleSync rule documents."""

from __future__ import annotations

import dataclasses
import operator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.infra.tools.review.context.rules_index import RuleItem


@dataclasses.dataclass(frozen=True)
class RubricCriterion:
    """A concrete evaluation item derived from a rule bullet."""

    rule_id: str
    title: str
    requirement: str
    domain: str
    source_path: str
    line_number: int


@dataclasses.dataclass(frozen=True)
class DomainRubric:
    """Grouped criteria for a specific domain (Architecture, Testing, etc.)."""

    domain: str
    criteria: list[RubricCriterion]


def synthesize_rubrics(applicable_rules: list[RuleItem]) -> list[DomainRubric]:
    """Synthesizes structured rubrics from applicable RuleItem definitions."""
    by_domain: dict[str, list[RubricCriterion]] = {}

    for rule in applicable_rules:
        crit = RubricCriterion(
            rule_id=rule.rule_id,
            title=rule.title,
            requirement=rule.description,
            domain=rule.domain,
            source_path=rule.path,
            line_number=rule.line_number,
        )
        by_domain.setdefault(rule.domain, []).append(crit)

    # Return sorted by domain name
    return [
        DomainRubric(domain=dom, criteria=crits)
        for dom, crits in sorted(by_domain.items(), key=operator.itemgetter(0))
    ]
