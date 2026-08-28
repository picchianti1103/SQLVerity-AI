from __future__ import annotations

import unittest

from packages.domain.sqlverity_domain.models import QueryRequestState
from packages.domain.sqlverity_domain.query_state import InvalidStateTransition, QueryLifecycle


class QueryLifecycleTests(unittest.TestCase):
    def test_preview_and_approval_are_required_before_execution(self) -> None:
        lifecycle = QueryLifecycle("request-1")
        for state in (
            QueryRequestState.CONTEXT_BUILDING,
            QueryRequestState.LLM_GENERATING,
            QueryRequestState.GENERATED,
            QueryRequestState.VALIDATING,
            QueryRequestState.READY_FOR_PREVIEW,
            QueryRequestState.APPROVED,
            QueryRequestState.EXECUTING,
        ):
            lifecycle = lifecycle.transition(state)

        self.assertEqual(QueryRequestState.EXECUTING, lifecycle.state)

    def test_execution_cannot_start_directly(self) -> None:
        lifecycle = QueryLifecycle("request-1")
        with self.assertRaises(InvalidStateTransition):
            lifecycle.transition(QueryRequestState.EXECUTING)


if __name__ == "__main__":
    unittest.main()

