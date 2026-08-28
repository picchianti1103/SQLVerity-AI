from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import Any
from uuid import UUID

from packages.domain.sqlverity_domain.models import (
    QueryParameterDefinition,
    QueryParameterType,
)


class QueryParameterBindingError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class BoundQueryParameters:
    values: Mapping[str, Any]
    names: tuple[str, ...]
    value_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))


def bind_query_parameters(
    definitions: tuple[QueryParameterDefinition, ...],
    supplied: Mapping[str, object],
) -> BoundQueryParameters:
    if not definitions:
        if supplied:
            raise QueryParameterBindingError(
                "This generated query does not declare execution parameters"
            )
        return BoundQueryParameters(values={}, names=(), value_hash=_empty_hash())
    if len(definitions) > 50:
        raise QueryParameterBindingError("A query can declare at most 50 parameters")

    by_name = {definition.name: definition for definition in definitions}
    expected = set(by_name)
    received = set(supplied)
    if expected != received:
        missing = ", ".join(sorted(expected - received)) or "none"
        unexpected = ", ".join(sorted(received - expected)) or "none"
        raise QueryParameterBindingError(
            f"Parameter bindings do not match declarations; missing: {missing}; "
            f"unexpected: {unexpected}"
        )

    values: dict[str, Any] = {}
    canonical: dict[str, object] = {}
    for name in sorted(expected):
        definition = by_name[name]
        value, canonical_value = _coerce(definition, supplied[name])
        values[name] = value
        canonical[name] = {
            "type": definition.value_type.value,
            "nullable": definition.nullable,
            "value": canonical_value,
        }
    serialized = json.dumps(
        canonical,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return BoundQueryParameters(
        values=values,
        names=tuple(sorted(expected)),
        value_hash=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
    )


def _coerce(
    definition: QueryParameterDefinition,
    value: object,
) -> tuple[Any, object]:
    name = definition.name
    if value is None:
        if not definition.nullable:
            raise QueryParameterBindingError(f"Query parameter {name} cannot be null")
        return None, None
    value_type = definition.value_type
    if value_type is QueryParameterType.STRING:
        if not isinstance(value, str):
            raise QueryParameterBindingError(f"Query parameter {name} must be a string")
        if len(value) > 10_000:
            raise QueryParameterBindingError(
                f"Query parameter {name} exceeds the 10000 character limit"
            )
        return value, value
    if value_type is QueryParameterType.INTEGER:
        if isinstance(value, bool) or not isinstance(value, int):
            raise QueryParameterBindingError(f"Query parameter {name} must be an integer")
        return value, value
    if value_type is QueryParameterType.NUMBER:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise QueryParameterBindingError(f"Query parameter {name} must be a number")
        if isinstance(value, float) and not math.isfinite(value):
            raise QueryParameterBindingError(f"Query parameter {name} must be finite")
        return value, value
    if value_type is QueryParameterType.BOOLEAN:
        if not isinstance(value, bool):
            raise QueryParameterBindingError(f"Query parameter {name} must be a boolean")
        return value, value
    if value_type is QueryParameterType.DATE:
        if not isinstance(value, str):
            raise QueryParameterBindingError(
                f"Query parameter {name} must be an ISO 8601 date"
            )
        try:
            parsed_date = date.fromisoformat(value)
        except ValueError:
            raise QueryParameterBindingError(
                f"Query parameter {name} must be an ISO 8601 date"
            ) from None
        return parsed_date, parsed_date.isoformat()
    if value_type is QueryParameterType.DATETIME:
        if not isinstance(value, str):
            raise QueryParameterBindingError(
                f"Query parameter {name} must be an ISO 8601 datetime"
            )
        try:
            parsed_datetime = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise QueryParameterBindingError(
                f"Query parameter {name} must be an ISO 8601 datetime"
            ) from None
        return parsed_datetime, parsed_datetime.isoformat()
    if value_type is QueryParameterType.UUID:
        if not isinstance(value, str):
            raise QueryParameterBindingError(f"Query parameter {name} must be a UUID")
        try:
            parsed_uuid = UUID(value)
        except ValueError:
            raise QueryParameterBindingError(f"Query parameter {name} must be a UUID") from None
        canonical_uuid = str(parsed_uuid)
        return canonical_uuid, canonical_uuid
    raise QueryParameterBindingError(f"Query parameter {name} has an unsupported type")


def _empty_hash() -> str:
    return hashlib.sha256(b"{}").hexdigest()
