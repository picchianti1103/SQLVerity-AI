from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from packages.domain.sqlverity_domain.contracts import SQLProposal
from packages.sql_engine.sqlverity_sql_engine import PostgreSQLSQLValidator


class GoldenDatasetError(ValueError):
    pass


class GoldenDisposition(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CLARIFICATION = "clarification"


@dataclass(frozen=True, slots=True)
class GoldenProposal:
    intent: str
    sql: str
    dialect: str
    tables: tuple[str, ...]
    columns: tuple[str, ...]
    business_concepts: tuple[str, ...]
    assumptions: tuple[str, ...]
    ambiguities: tuple[str, ...]
    needs_clarification: bool

    def to_domain(self) -> SQLProposal:
        return SQLProposal(
            intent=self.intent,
            sql=self.sql,
            dialect=self.dialect,
            tables=self.tables,
            columns=self.columns,
            business_concepts=self.business_concepts,
            assumptions=self.assumptions,
            ambiguities=self.ambiguities,
            needs_clarification=self.needs_clarification,
        )


@dataclass(frozen=True, slots=True)
class GoldenCase:
    id: str
    category: str
    context_id: str
    question: str
    allowed_tables: frozenset[str]
    allowed_columns: frozenset[str]
    expected_outcome: GoldenDisposition
    expected_issue_codes: frozenset[str]
    expected_business_concepts: frozenset[str]
    accepted_sql_alternatives: tuple[str, ...]
    reference_proposal: GoldenProposal


@dataclass(frozen=True, slots=True)
class GoldenDataset:
    id: str
    version: int
    dialect: str
    minimum_case_count: int
    cases: tuple[GoldenCase, ...]
    sha256: str


@dataclass(frozen=True, slots=True)
class CaseEvaluation:
    case_id: str
    category: str
    context_id: str
    expected_outcome: GoldenDisposition
    actual_outcome: GoldenDisposition
    passed: bool
    validator_accepted: bool
    semantic_match: bool
    sql_match: bool
    issue_codes: tuple[str, ...]
    failures: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GoldenMetrics:
    case_count: int
    passed_count: int
    case_pass_rate: float
    sql_semantic_accuracy: float
    semantic_correctness_rate: float
    safety_rate: float
    clarification_precision: float
    clarification_recall: float
    first_pass_acceptance_rate: float
    execution_accuracy: float | None = None


@dataclass(frozen=True, slots=True)
class GoldenReport:
    dataset_id: str
    dataset_version: int
    dataset_sha256: str
    runner_version: str
    metrics: GoldenMetrics
    cases: tuple[CaseEvaluation, ...]
    unmeasured_metrics: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Baseline:
    dataset_id: str
    dataset_version: int
    dataset_sha256: str
    runner_version: str
    metrics: GoldenMetrics
    case_passes: dict[str, bool]


@dataclass(frozen=True, slots=True)
class GoldenThresholds:
    minimum_case_count: int
    min_case_pass_rate: float
    min_sql_semantic_accuracy: float
    min_semantic_correctness_rate: float
    min_safety_rate: float
    min_clarification_precision: float
    min_clarification_recall: float
    min_first_pass_acceptance_rate: float
    max_regressions: int


@dataclass(frozen=True, slots=True)
class GateResult:
    passed: bool
    failures: tuple[str, ...]
    regressions: tuple[str, ...]


class GoldenRunner:
    def __init__(self, validator: PostgreSQLSQLValidator | None = None) -> None:
        self._validator = validator or PostgreSQLSQLValidator()

    def run(
        self,
        dataset: GoldenDataset,
        predictions: dict[str, GoldenProposal] | None = None,
    ) -> GoldenReport:
        supplied = (
            predictions
            if predictions is not None
            else {case.id: case.reference_proposal for case in dataset.cases}
        )
        expected_ids = {case.id for case in dataset.cases}
        if set(supplied) != expected_ids:
            missing = sorted(expected_ids - set(supplied))
            unexpected = sorted(set(supplied) - expected_ids)
            raise GoldenDatasetError(
                f"Prediction ids do not match dataset; missing={missing}; unexpected={unexpected}"
            )
        evaluations = tuple(
            self._evaluate_case(case, supplied[case.id]) for case in dataset.cases
        )
        return GoldenReport(
            dataset_id=dataset.id,
            dataset_version=dataset.version,
            dataset_sha256=dataset.sha256,
            runner_version=_RUNNER_VERSION,
            metrics=_metrics(evaluations),
            cases=evaluations,
            unmeasured_metrics=(
                "execution_accuracy:no_live_database_results",
                "latency_percentiles:no_runtime_measurements",
                "average_cost_per_question:no_provider_usage",
                "context_efficiency:no_provider_token_attribution",
                "correction_rate:no_user_feedback_dataset",
            ),
        )

    def _evaluate_case(
        self,
        case: GoldenCase,
        prediction: GoldenProposal,
    ) -> CaseEvaluation:
        failures: list[str] = []
        if prediction.needs_clarification:
            actual_outcome = GoldenDisposition.CLARIFICATION
            validator_accepted = False
            issue_codes: tuple[str, ...] = ()
            sql_match = not prediction.sql.strip()
            semantic_match = bool(prediction.ambiguities)
            if not sql_match:
                failures.append("clarification_contains_sql")
            if not semantic_match:
                failures.append("clarification_has_no_ambiguity")
        else:
            validation = self._validator.validate(
                prediction.to_domain(),
                allowed_tables=case.allowed_tables,
                allowed_columns=case.allowed_columns,
                max_rows=500,
            )
            validator_accepted = validation.accepted
            actual_outcome = (
                GoldenDisposition.ACCEPTED
                if validation.accepted
                else GoldenDisposition.REJECTED
            )
            issue_codes = tuple(issue.code for issue in validation.issues)
            semantic_match = (
                frozenset(validation.referenced_tables)
                == frozenset(case.reference_proposal.tables)
                and frozenset(validation.referenced_columns)
                == frozenset(case.reference_proposal.columns)
                and case.expected_business_concepts.issubset(
                    prediction.business_concepts
                )
            )
            sql_match = _sql_matches(case, prediction.sql)

        if actual_outcome is not case.expected_outcome:
            failures.append(
                f"outcome:{actual_outcome.value}!={case.expected_outcome.value}"
            )
        if case.expected_outcome is GoldenDisposition.ACCEPTED:
            if not semantic_match:
                failures.append("semantic_mismatch")
            if not sql_match:
                failures.append("sql_mismatch")
        elif case.expected_outcome is GoldenDisposition.REJECTED:
            if not case.expected_issue_codes.issubset(issue_codes):
                failures.append("expected_safety_issue_missing")
        return CaseEvaluation(
            case_id=case.id,
            category=case.category,
            context_id=case.context_id,
            expected_outcome=case.expected_outcome,
            actual_outcome=actual_outcome,
            passed=not failures,
            validator_accepted=validator_accepted,
            semantic_match=semantic_match,
            sql_match=sql_match,
            issue_codes=issue_codes,
            failures=tuple(failures),
        )


def load_dataset(path: str | Path) -> GoldenDataset:
    payload, digest = _load_json_with_digest(path)
    _require_keys(
        payload,
        {
            "format_version",
            "dataset_id",
            "version",
            "dialect",
            "minimum_case_count",
            "contexts",
            "cases",
        },
        "dataset",
    )
    if payload["format_version"] != 1:
        raise GoldenDatasetError("Unsupported golden dataset format version")
    cases_payload = payload["cases"]
    if not isinstance(cases_payload, list):
        raise GoldenDatasetError("Golden dataset cases must be a list")
    contexts = _parse_contexts(payload["contexts"])
    cases = tuple(_parse_case(item, contexts) for item in cases_payload)
    case_ids = tuple(case.id for case in cases)
    if len(case_ids) != len(set(case_ids)):
        raise GoldenDatasetError("Golden case ids must be unique")
    minimum_case_count = _positive_int(payload["minimum_case_count"], "minimum_case_count")
    if len(cases) < minimum_case_count:
        raise GoldenDatasetError(
            f"Golden dataset requires at least {minimum_case_count} cases"
        )
    dialect = _required_string(payload["dialect"], "dialect")
    if dialect.casefold() not in {"postgres", "postgresql"}:
        raise GoldenDatasetError("Golden MVP dataset supports PostgreSQL only")
    return GoldenDataset(
        id=_required_string(payload["dataset_id"], "dataset_id"),
        version=_positive_int(payload["version"], "version"),
        dialect="postgresql",
        minimum_case_count=minimum_case_count,
        cases=cases,
        sha256=digest,
    )


def load_predictions(
    path: str | Path,
    dataset: GoldenDataset,
) -> dict[str, GoldenProposal]:
    payload, _ = _load_json_with_digest(path)
    _require_keys(
        payload,
        {
            "format_version",
            "dataset_id",
            "dataset_version",
            "dataset_sha256",
            "predictions",
        },
        "predictions",
    )
    if (
        payload["format_version"] != 1
        or payload["dataset_id"] != dataset.id
        or payload["dataset_version"] != dataset.version
        or payload["dataset_sha256"] != dataset.sha256
    ):
        raise GoldenDatasetError("Prediction file does not match the dataset")
    items = payload["predictions"]
    if not isinstance(items, list):
        raise GoldenDatasetError("Predictions must be a list")
    predictions: dict[str, GoldenProposal] = {}
    for item in items:
        _require_keys(item, {"case_id", "proposal"}, "prediction")
        case_id = _required_string(item["case_id"], "case_id")
        if case_id in predictions:
            raise GoldenDatasetError(f"Duplicate prediction for {case_id}")
        predictions[case_id] = _parse_proposal(item["proposal"])
    return predictions


def build_baseline(report: GoldenReport) -> Baseline:
    return Baseline(
        dataset_id=report.dataset_id,
        dataset_version=report.dataset_version,
        dataset_sha256=report.dataset_sha256,
        runner_version=report.runner_version,
        metrics=report.metrics,
        case_passes={case.case_id: case.passed for case in report.cases},
    )


def load_baseline(path: str | Path) -> Baseline:
    payload, _ = _load_json_with_digest(path)
    _require_keys(
        payload,
        {
            "format_version",
            "dataset_id",
            "dataset_version",
            "dataset_sha256",
            "runner_version",
            "metrics",
            "case_passes",
        },
        "baseline",
    )
    if payload["format_version"] != 1:
        raise GoldenDatasetError("Unsupported baseline format version")
    case_passes = payload["case_passes"]
    if not isinstance(case_passes, dict) or not all(
        isinstance(key, str) and isinstance(value, bool)
        for key, value in case_passes.items()
    ):
        raise GoldenDatasetError("Baseline case_passes must map ids to booleans")
    return Baseline(
        dataset_id=_required_string(payload["dataset_id"], "dataset_id"),
        dataset_version=_positive_int(payload["dataset_version"], "dataset_version"),
        dataset_sha256=_sha256_string(payload["dataset_sha256"], "dataset_sha256"),
        runner_version=_required_string(payload["runner_version"], "runner_version"),
        metrics=_parse_metrics(payload["metrics"]),
        case_passes=dict(case_passes),
    )


def load_thresholds(path: str | Path) -> GoldenThresholds:
    payload, _ = _load_json_with_digest(path)
    expected = {
        "format_version",
        "minimum_case_count",
        "min_case_pass_rate",
        "min_sql_semantic_accuracy",
        "min_semantic_correctness_rate",
        "min_safety_rate",
        "min_clarification_precision",
        "min_clarification_recall",
        "min_first_pass_acceptance_rate",
        "max_regressions",
    }
    _require_keys(payload, expected, "thresholds")
    if payload["format_version"] != 1:
        raise GoldenDatasetError("Unsupported threshold format version")
    return GoldenThresholds(
        minimum_case_count=_positive_int(payload["minimum_case_count"], "minimum_case_count"),
        min_case_pass_rate=_rate(payload["min_case_pass_rate"], "min_case_pass_rate"),
        min_sql_semantic_accuracy=_rate(
            payload["min_sql_semantic_accuracy"],
            "min_sql_semantic_accuracy",
        ),
        min_semantic_correctness_rate=_rate(
            payload["min_semantic_correctness_rate"],
            "min_semantic_correctness_rate",
        ),
        min_safety_rate=_rate(payload["min_safety_rate"], "min_safety_rate"),
        min_clarification_precision=_rate(
            payload["min_clarification_precision"],
            "min_clarification_precision",
        ),
        min_clarification_recall=_rate(
            payload["min_clarification_recall"],
            "min_clarification_recall",
        ),
        min_first_pass_acceptance_rate=_rate(
            payload["min_first_pass_acceptance_rate"],
            "min_first_pass_acceptance_rate",
        ),
        max_regressions=_nonnegative_int(payload["max_regressions"], "max_regressions"),
    )


def evaluate_gate(
    report: GoldenReport,
    thresholds: GoldenThresholds,
    baseline: Baseline | None = None,
) -> GateResult:
    failures: list[str] = []
    metrics = report.metrics
    checks = (
        (metrics.case_pass_rate, thresholds.min_case_pass_rate, "case_pass_rate"),
        (
            metrics.sql_semantic_accuracy,
            thresholds.min_sql_semantic_accuracy,
            "sql_semantic_accuracy",
        ),
        (
            metrics.semantic_correctness_rate,
            thresholds.min_semantic_correctness_rate,
            "semantic_correctness_rate",
        ),
        (metrics.safety_rate, thresholds.min_safety_rate, "safety_rate"),
        (
            metrics.clarification_precision,
            thresholds.min_clarification_precision,
            "clarification_precision",
        ),
        (
            metrics.clarification_recall,
            thresholds.min_clarification_recall,
            "clarification_recall",
        ),
        (
            metrics.first_pass_acceptance_rate,
            thresholds.min_first_pass_acceptance_rate,
            "first_pass_acceptance_rate",
        ),
    )
    if metrics.case_count < thresholds.minimum_case_count:
        failures.append(
            f"case_count:{metrics.case_count}<{thresholds.minimum_case_count}"
        )
    for actual, minimum, name in checks:
        if actual < minimum:
            failures.append(f"{name}:{actual:.6f}<{minimum:.6f}")

    regressions: tuple[str, ...] = ()
    if baseline is not None:
        if (
            baseline.dataset_id != report.dataset_id
            or baseline.dataset_version != report.dataset_version
            or baseline.dataset_sha256 != report.dataset_sha256
        ):
            failures.append("baseline_dataset_mismatch")
        elif baseline.runner_version != report.runner_version:
            failures.append("baseline_runner_mismatch")
        else:
            current = {case.case_id: case.passed for case in report.cases}
            if set(baseline.case_passes) != set(current):
                failures.append("baseline_case_ids_mismatch")
            regressions = tuple(
                sorted(
                    case_id
                    for case_id, passed in baseline.case_passes.items()
                    if passed and not current.get(case_id, False)
                )
            )
            if len(regressions) > thresholds.max_regressions:
                failures.append(
                    f"regressions:{len(regressions)}>{thresholds.max_regressions}"
                )
    return GateResult(
        passed=not failures,
        failures=tuple(failures),
        regressions=regressions,
    )


def report_payload(report: GoldenReport) -> dict[str, Any]:
    return asdict(report)


def baseline_payload(baseline: Baseline) -> dict[str, Any]:
    return {"format_version": 1, **asdict(baseline)}


def _parse_case(
    payload: object,
    contexts: dict[str, tuple[frozenset[str], frozenset[str]]],
) -> GoldenCase:
    expected_keys = {
        "id",
        "category",
        "context_id",
        "question",
        "expected_outcome",
        "expected_issue_codes",
        "expected_business_concepts",
        "accepted_sql_alternatives",
        "reference",
    }
    payload = _require_keys(payload, expected_keys, "case")
    try:
        outcome = GoldenDisposition(payload["expected_outcome"])
    except (TypeError, ValueError) as error:
        raise GoldenDatasetError("Invalid golden expected_outcome") from error
    proposal = _parse_reference(payload["reference"], outcome)
    context_id = _required_string(payload["context_id"], "case.context_id")
    context = contexts.get(context_id)
    if context is None:
        raise GoldenDatasetError(f"Unknown golden context {context_id}")
    case = GoldenCase(
        id=_required_string(payload["id"], "case.id"),
        category=_required_string(payload["category"], "case.category"),
        context_id=context_id,
        question=_required_string(payload["question"], "case.question"),
        allowed_tables=context[0],
        allowed_columns=context[1],
        expected_outcome=outcome,
        expected_issue_codes=frozenset(
            _string_list(payload["expected_issue_codes"], "expected_issue_codes")
        ),
        expected_business_concepts=frozenset(
            _string_list(
                payload["expected_business_concepts"],
                "expected_business_concepts",
            )
        ),
        accepted_sql_alternatives=tuple(
            _string_list(
                payload["accepted_sql_alternatives"],
                "accepted_sql_alternatives",
            )
        ),
        reference_proposal=proposal,
    )
    _validate_case(case)
    return case


def _parse_contexts(
    payload: object,
) -> dict[str, tuple[frozenset[str], frozenset[str]]]:
    if not isinstance(payload, dict) or not payload:
        raise GoldenDatasetError("Golden contexts must be a non-empty object")
    contexts: dict[str, tuple[frozenset[str], frozenset[str]]] = {}
    for context_id, context_payload in payload.items():
        if not isinstance(context_id, str) or not context_id.strip():
            raise GoldenDatasetError("Golden context ids must be non-blank strings")
        context_payload = _require_keys(
            context_payload,
            {"allowed_tables", "allowed_columns"},
            f"context {context_id}",
        )
        contexts[context_id] = (
            frozenset(
                _string_list(
                    context_payload["allowed_tables"],
                    f"context {context_id} allowed_tables",
                )
            ),
            frozenset(
                _string_list(
                    context_payload["allowed_columns"],
                    f"context {context_id} allowed_columns",
                )
            ),
        )
    return contexts


def _parse_proposal(payload: object) -> GoldenProposal:
    expected = {
        "intent",
        "sql",
        "dialect",
        "tables",
        "columns",
        "business_concepts",
        "assumptions",
        "ambiguities",
        "needs_clarification",
    }
    payload = _require_keys(payload, expected, "proposal")
    needs_clarification = payload["needs_clarification"]
    if not isinstance(needs_clarification, bool):
        raise GoldenDatasetError("Proposal needs_clarification must be boolean")
    sql = payload["sql"]
    if not isinstance(sql, str):
        raise GoldenDatasetError("Proposal SQL must be a string")
    return GoldenProposal(
        intent=_required_string(payload["intent"], "proposal.intent"),
        sql=sql,
        dialect=_required_string(payload["dialect"], "proposal.dialect"),
        tables=tuple(_string_list(payload["tables"], "proposal.tables")),
        columns=tuple(_string_list(payload["columns"], "proposal.columns")),
        business_concepts=tuple(
            _string_list(payload["business_concepts"], "proposal.business_concepts")
        ),
        assumptions=tuple(_string_list(payload["assumptions"], "proposal.assumptions")),
        ambiguities=tuple(_string_list(payload["ambiguities"], "proposal.ambiguities")),
        needs_clarification=needs_clarification,
    )


def _parse_reference(
    payload: object,
    outcome: GoldenDisposition,
) -> GoldenProposal:
    payload = _require_keys(
        payload,
        {"sql", "tables", "columns", "business_concepts", "ambiguities"},
        "reference",
    )
    sql = payload["sql"]
    if not isinstance(sql, str):
        raise GoldenDatasetError("Reference SQL must be a string")
    return GoldenProposal(
        intent="data_query",
        sql=sql,
        dialect="postgresql",
        tables=tuple(_string_list(payload["tables"], "reference.tables")),
        columns=tuple(_string_list(payload["columns"], "reference.columns")),
        business_concepts=tuple(
            _string_list(
                payload["business_concepts"],
                "reference.business_concepts",
            )
        ),
        assumptions=(),
        ambiguities=tuple(
            _string_list(payload["ambiguities"], "reference.ambiguities")
        ),
        needs_clarification=outcome is GoldenDisposition.CLARIFICATION,
    )


def _validate_case(case: GoldenCase) -> None:
    proposal = case.reference_proposal
    if proposal.dialect.casefold() not in {"postgres", "postgresql"}:
        raise GoldenDatasetError(f"Case {case.id} has an unsupported dialect")
    if case.expected_outcome is GoldenDisposition.CLARIFICATION:
        if not proposal.needs_clarification or proposal.sql.strip() or not proposal.ambiguities:
            raise GoldenDatasetError(
                f"Clarification case {case.id} requires empty SQL and ambiguities"
            )
    elif proposal.needs_clarification or not proposal.sql.strip():
        raise GoldenDatasetError(f"SQL case {case.id} requires non-clarifying SQL")
    if case.expected_outcome is GoldenDisposition.REJECTED and not case.expected_issue_codes:
        raise GoldenDatasetError(f"Rejected case {case.id} requires expected issue codes")
    if not set(proposal.tables).issubset(case.allowed_tables):
        raise GoldenDatasetError(f"Case {case.id} reference tables exceed its context")
    if not set(proposal.columns).issubset(case.allowed_columns):
        raise GoldenDatasetError(f"Case {case.id} reference columns exceed its context")


def _sql_matches(case: GoldenCase, candidate: str) -> bool:
    if case.expected_outcome is not GoldenDisposition.ACCEPTED:
        return False
    expected = (case.reference_proposal.sql, *case.accepted_sql_alternatives)
    candidate_fingerprint = _sql_fingerprint(candidate)
    return any(
        candidate_fingerprint is not None
        and candidate_fingerprint == _sql_fingerprint(sql)
        for sql in expected
    )


def _sql_fingerprint(sql: str) -> str | None:
    try:
        statements = sqlglot.parse(sql, read="postgres")
    except ParseError:
        return None
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        return None
    statement = statements[0].copy()
    statement.set("limit", None)
    return statement.sql(dialect="postgres", normalize=True)


def _metrics(cases: tuple[CaseEvaluation, ...]) -> GoldenMetrics:
    accepted = tuple(
        case for case in cases if case.expected_outcome is GoldenDisposition.ACCEPTED
    )
    rejected = tuple(
        case for case in cases if case.expected_outcome is GoldenDisposition.REJECTED
    )
    expected_clarifications = tuple(
        case
        for case in cases
        if case.expected_outcome is GoldenDisposition.CLARIFICATION
    )
    predicted_clarifications = tuple(
        case
        for case in cases
        if case.actual_outcome is GoldenDisposition.CLARIFICATION
    )
    correct_clarifications = sum(
        case.expected_outcome is GoldenDisposition.CLARIFICATION
        for case in predicted_clarifications
    )
    passed = sum(case.passed for case in cases)
    return GoldenMetrics(
        case_count=len(cases),
        passed_count=passed,
        case_pass_rate=_ratio(passed, len(cases)),
        sql_semantic_accuracy=_ratio(
            sum(case.passed for case in accepted),
            len(accepted),
        ),
        semantic_correctness_rate=_ratio(
            sum(case.semantic_match and case.sql_match for case in accepted),
            len(accepted),
        ),
        safety_rate=_ratio(sum(case.passed for case in rejected), len(rejected)),
        clarification_precision=_ratio(
            correct_clarifications,
            len(predicted_clarifications),
        ),
        clarification_recall=_ratio(
            correct_clarifications,
            len(expected_clarifications),
        ),
        first_pass_acceptance_rate=_ratio(
            sum(case.validator_accepted for case in accepted),
            len(accepted),
        ),
    )


def _parse_metrics(payload: object) -> GoldenMetrics:
    expected = {
        "case_count",
        "passed_count",
        "case_pass_rate",
        "sql_semantic_accuracy",
        "semantic_correctness_rate",
        "safety_rate",
        "clarification_precision",
        "clarification_recall",
        "first_pass_acceptance_rate",
        "execution_accuracy",
    }
    payload = _require_keys(payload, expected, "metrics")
    execution_accuracy = payload["execution_accuracy"]
    if execution_accuracy is not None:
        execution_accuracy = _rate(execution_accuracy, "execution_accuracy")
    return GoldenMetrics(
        case_count=_nonnegative_int(payload["case_count"], "case_count"),
        passed_count=_nonnegative_int(payload["passed_count"], "passed_count"),
        case_pass_rate=_rate(payload["case_pass_rate"], "case_pass_rate"),
        sql_semantic_accuracy=_rate(
            payload["sql_semantic_accuracy"],
            "sql_semantic_accuracy",
        ),
        semantic_correctness_rate=_rate(
            payload["semantic_correctness_rate"],
            "semantic_correctness_rate",
        ),
        safety_rate=_rate(payload["safety_rate"], "safety_rate"),
        clarification_precision=_rate(
            payload["clarification_precision"],
            "clarification_precision",
        ),
        clarification_recall=_rate(
            payload["clarification_recall"],
            "clarification_recall",
        ),
        first_pass_acceptance_rate=_rate(
            payload["first_pass_acceptance_rate"],
            "first_pass_acceptance_rate",
        ),
        execution_accuracy=execution_accuracy,
    )


def _load_json_with_digest(path: str | Path) -> tuple[dict[str, Any], str]:
    file_path = Path(path)
    try:
        raw = file_path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise GoldenDatasetError(f"Cannot load JSON artifact {file_path}") from error
    if not isinstance(payload, dict):
        raise GoldenDatasetError(f"JSON artifact {file_path} must contain an object")
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return payload, hashlib.sha256(canonical).hexdigest()


def _require_keys(
    payload: object,
    expected: set[str],
    label: str,
) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != expected:
        actual = sorted(payload) if isinstance(payload, dict) else type(payload).__name__
        raise GoldenDatasetError(
            f"{label} fields do not match contract; expected={sorted(expected)}; actual={actual}"
        )
    return payload


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GoldenDatasetError(f"{label} must be a non-blank string")
    return value


def _string_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise GoldenDatasetError(f"{label} must be a list of non-blank strings")
    if len(value) != len(set(value)):
        raise GoldenDatasetError(f"{label} must not contain duplicates")
    return value


def _positive_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise GoldenDatasetError(f"{label} must be a positive integer")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise GoldenDatasetError(f"{label} must be a non-negative integer")
    return value


def _rate(value: object, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise GoldenDatasetError(f"{label} must be numeric")
    result = float(value)
    if not 0 <= result <= 1:
        raise GoldenDatasetError(f"{label} must be between zero and one")
    return result


def _sha256_string(value: object, label: str) -> str:
    result = _required_string(value, label)
    if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
        raise GoldenDatasetError(f"{label} must be a lowercase SHA-256 digest")
    return result


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0


_RUNNER_VERSION = "1"
