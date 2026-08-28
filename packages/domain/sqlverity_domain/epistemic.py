from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .models import (
    BusinessConceptDefinition,
    BusinessConceptResolution,
    BusinessRuleDefinition,
    BusinessRuleResolution,
    EpistemicStatus,
    MetricDefinition,
    MetricResolution,
    SemanticDefinition,
    SemanticResolution,
)


class ResolutionAction(StrEnum):
    ACCEPT = "accept"
    KEEP_CURRENT = "keep_current"
    MARK_CONFLICT = "mark_conflict"


@dataclass(frozen=True, slots=True)
class ResolutionDecision:
    action: ResolutionAction
    reason: str


_PRECEDENCE = {
    EpistemicStatus.UNKNOWN: 0,
    EpistemicStatus.INFERRED: 1,
    EpistemicStatus.IMPORTED: 2,
    EpistemicStatus.CONFIRMED: 3,
}


def resolve_semantic_update(
    current: SemanticResolution | None,
    candidate: SemanticDefinition,
) -> ResolutionDecision:
    """Apply the document's epistemic precedence without silently rewriting knowledge."""

    if current is None or current.status is EpistemicStatus.UNKNOWN:
        return ResolutionDecision(ResolutionAction.ACCEPT, "No governed definition exists")

    if current.status is EpistemicStatus.CONFLICTING:
        if candidate.status is EpistemicStatus.CONFIRMED:
            return ResolutionDecision(
                ResolutionAction.ACCEPT,
                "An explicit confirmation resolves the existing conflict",
            )
        return ResolutionDecision(
            ResolutionAction.KEEP_CURRENT,
            "Only a confirmed definition can resolve a conflict",
        )

    current_rank = _PRECEDENCE[current.status]
    candidate_rank = _PRECEDENCE[candidate.status]

    if candidate.description.strip().casefold() == current.description.strip().casefold():
        if candidate_rank >= current_rank:
            return ResolutionDecision(ResolutionAction.ACCEPT, "Definition is corroborated")
        return ResolutionDecision(
            ResolutionAction.KEEP_CURRENT,
            "Equivalent lower-priority evidence was retained without replacing the resolution",
        )

    if candidate_rank < current_rank:
        return ResolutionDecision(
            ResolutionAction.KEEP_CURRENT,
            "Lower-priority evidence cannot overwrite governed knowledge",
        )
    if candidate_rank == current_rank:
        return ResolutionDecision(
            ResolutionAction.MARK_CONFLICT,
            "Different definitions with equal authority require human resolution",
        )
    return ResolutionDecision(
        ResolutionAction.ACCEPT,
        "Higher-priority evidence supersedes the resolution",
    )


def resolve_business_concept_update(
    current: BusinessConceptResolution | None,
    candidate: BusinessConceptDefinition,
) -> ResolutionDecision:
    """Resolve whole business-concept definitions without merging partial truths."""

    if current is None or current.status is EpistemicStatus.UNKNOWN:
        return ResolutionDecision(ResolutionAction.ACCEPT, "No governed concept exists")
    if current.status is EpistemicStatus.CONFLICTING:
        if candidate.status is EpistemicStatus.CONFIRMED:
            return ResolutionDecision(
                ResolutionAction.ACCEPT,
                "An explicit confirmation resolves the existing concept conflict",
            )
        return ResolutionDecision(
            ResolutionAction.KEEP_CURRENT,
            "Only a confirmed definition can resolve a concept conflict",
        )

    current_rank = _PRECEDENCE[current.status]
    candidate_rank = _PRECEDENCE[candidate.status]
    if _concept_payload(current) == _concept_payload(candidate):
        if candidate_rank >= current_rank:
            return ResolutionDecision(ResolutionAction.ACCEPT, "Concept is corroborated")
        return ResolutionDecision(
            ResolutionAction.KEEP_CURRENT,
            "Equivalent lower-priority evidence was retained",
        )
    if candidate_rank < current_rank:
        return ResolutionDecision(
            ResolutionAction.KEEP_CURRENT,
            "Lower-priority evidence cannot overwrite the governed concept",
        )
    if candidate_rank == current_rank:
        return ResolutionDecision(
            ResolutionAction.MARK_CONFLICT,
            "Different concept definitions with equal authority require review",
        )
    return ResolutionDecision(
        ResolutionAction.ACCEPT,
        "Higher-priority evidence supersedes the concept resolution",
    )


def _concept_payload(
    concept: BusinessConceptDefinition | BusinessConceptResolution,
) -> tuple[object, ...]:
    return (
        concept.name.strip().casefold(),
        concept.description.strip().casefold(),
        tuple(sorted(value.strip().casefold() for value in concept.synonyms)),
        tuple(sorted(concept.object_refs)),
        concept.content_classification,
    )


def resolve_metric_update(
    current: MetricResolution | None,
    candidate: MetricDefinition,
) -> ResolutionDecision:
    return _resolve_analytic_update(
        current,
        candidate.status,
        _metric_payload(current) if current is not None else None,
        _metric_payload(candidate),
        "metric",
    )


def resolve_business_rule_update(
    current: BusinessRuleResolution | None,
    candidate: BusinessRuleDefinition,
) -> ResolutionDecision:
    return _resolve_analytic_update(
        current,
        candidate.status,
        _rule_payload(current) if current is not None else None,
        _rule_payload(candidate),
        "business rule",
    )


def _resolve_analytic_update(
    current: MetricResolution | BusinessRuleResolution | None,
    candidate_status: EpistemicStatus,
    current_payload: tuple[object, ...] | None,
    candidate_payload: tuple[object, ...],
    label: str,
) -> ResolutionDecision:
    if current is None or current.status is EpistemicStatus.UNKNOWN:
        return ResolutionDecision(ResolutionAction.ACCEPT, f"No governed {label} exists")
    if current.status is EpistemicStatus.CONFLICTING:
        if candidate_status is EpistemicStatus.CONFIRMED:
            return ResolutionDecision(
                ResolutionAction.ACCEPT,
                f"An explicit confirmation resolves the {label} conflict",
            )
        return ResolutionDecision(
            ResolutionAction.KEEP_CURRENT,
            f"Only confirmed evidence can resolve a {label} conflict",
        )
    current_rank = _PRECEDENCE[current.status]
    candidate_rank = _PRECEDENCE[candidate_status]
    if current_payload == candidate_payload:
        if candidate_rank >= current_rank:
            return ResolutionDecision(ResolutionAction.ACCEPT, f"The {label} is corroborated")
        return ResolutionDecision(ResolutionAction.KEEP_CURRENT, "Lower evidence was retained")
    if candidate_rank < current_rank:
        return ResolutionDecision(
            ResolutionAction.KEEP_CURRENT,
            f"Lower-priority evidence cannot overwrite the governed {label}",
        )
    if candidate_rank == current_rank:
        return ResolutionDecision(
            ResolutionAction.MARK_CONFLICT,
            f"Different {label} definitions with equal authority require review",
        )
    return ResolutionDecision(
        ResolutionAction.ACCEPT,
        f"Higher-priority evidence supersedes the {label} resolution",
    )


def _metric_payload(metric: MetricDefinition | MetricResolution) -> tuple[object, ...]:
    return (
        metric.name.strip().casefold(),
        metric.description.strip().casefold(),
        metric.normalized_expression_sql,
        tuple(sorted(metric.object_refs)),
        tuple(sorted(metric.grain_refs)),
        tuple(sorted(metric.dimension_refs)),
        tuple(sorted(metric.concept_keys)),
        tuple(sorted(metric.rule_keys)),
        metric.content_classification,
    )


def _rule_payload(
    rule: BusinessRuleDefinition | BusinessRuleResolution,
) -> tuple[object, ...]:
    return (
        rule.name.strip().casefold(),
        rule.description.strip().casefold(),
        rule.normalized_predicate_sql,
        tuple(sorted(rule.object_refs)),
        tuple(sorted(rule.concept_keys)),
        rule.content_classification,
    )
