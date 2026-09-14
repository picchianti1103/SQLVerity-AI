from __future__ import annotations

from sqlglot import exp


def normalize_identifiers(statement: exp.Expr, dialect: str) -> None:
    """Normalize SQL spelling, never catalog names or quoted identifiers.

    MySQL and SQL Server depend on server/filesystem/collation settings that the
    catalog does not currently capture. Require exact spelling there instead of
    guessing case-insensitive identity. Ignore SQLGlot metadata in SQL comments.
    """
    for identifier in statement.find_all(exp.Identifier):
        if identifier.quoted:
            continue
        if dialect == "postgres":
            identifier.set("this", identifier.name.lower())
        elif dialect == "oracle":
            identifier.set("this", identifier.name.upper())
