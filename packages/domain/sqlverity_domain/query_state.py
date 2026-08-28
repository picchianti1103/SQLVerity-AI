from __future__ import annotations

from dataclasses import dataclass

from .models import QueryRequestState


class InvalidStateTransition(ValueError):
    pass


_ALLOWED_TRANSITIONS: dict[QueryRequestState, frozenset[QueryRequestState]] = {
    QueryRequestState.RECEIVED: frozenset(
        {
            QueryRequestState.CONTEXT_BUILDING,
            QueryRequestState.CANCELLED,
            QueryRequestState.POLICY_BLOCKED,
        }
    ),
    QueryRequestState.CONTEXT_BUILDING: frozenset(
        {
            QueryRequestState.NEEDS_CLARIFICATION,
            QueryRequestState.LLM_GENERATING,
            QueryRequestState.POLICY_BLOCKED,
            QueryRequestState.CANCELLED,
        }
    ),
    QueryRequestState.NEEDS_CLARIFICATION: frozenset(
        {QueryRequestState.CONTEXT_BUILDING, QueryRequestState.CANCELLED}
    ),
    QueryRequestState.LLM_GENERATING: frozenset(
        {QueryRequestState.GENERATED, QueryRequestState.FAILED_LLM, QueryRequestState.CANCELLED}
    ),
    QueryRequestState.GENERATED: frozenset({QueryRequestState.VALIDATING}),
    QueryRequestState.VALIDATING: frozenset(
        {
            QueryRequestState.REJECTED,
            QueryRequestState.READY_FOR_PREVIEW,
            QueryRequestState.FAILED_VALIDATION,
            QueryRequestState.POLICY_BLOCKED,
        }
    ),
    QueryRequestState.READY_FOR_PREVIEW: frozenset(
        {QueryRequestState.APPROVED, QueryRequestState.CANCELLED}
    ),
    QueryRequestState.APPROVED: frozenset(
        {QueryRequestState.EXECUTING, QueryRequestState.CANCELLED}
    ),
    QueryRequestState.EXECUTING: frozenset(
        {
            QueryRequestState.SUCCEEDED,
            QueryRequestState.FAILED_EXECUTION,
            QueryRequestState.CANCELLED,
        }
    ),
    QueryRequestState.SUCCEEDED: frozenset({QueryRequestState.RESULT_PROCESSING}),
    QueryRequestState.RESULT_PROCESSING: frozenset(
        {QueryRequestState.COMPLETED, QueryRequestState.FAILED_EXECUTION}
    ),
}


@dataclass(frozen=True, slots=True)
class QueryLifecycle:
    request_id: str
    state: QueryRequestState = QueryRequestState.RECEIVED

    def transition(self, target: QueryRequestState) -> QueryLifecycle:
        allowed = _ALLOWED_TRANSITIONS.get(self.state, frozenset())
        if target not in allowed:
            raise InvalidStateTransition(f"Cannot transition from {self.state} to {target}")
        return QueryLifecycle(request_id=self.request_id, state=target)
