from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from packages.domain.sqlverity_domain.contracts import (
    ColumnSnapshot,
    DataSourceSnapshot,
    RelationshipSnapshot,
    SchemaObjectSnapshot,
)
from packages.domain.sqlverity_domain.models import ObjectKind


class DDLParseError(ValueError):
    pass


@dataclass(slots=True)
class _ColumnBuilder:
    name: str
    physical_type: str
    ordinal: int
    nullable: bool = True
    default_expression: str | None = None
    is_primary_key: bool = False
    comment: str | None = None


@dataclass(slots=True)
class _ObjectBuilder:
    schema_name: str
    name: str
    kind: ObjectKind
    columns: list[_ColumnBuilder] = field(default_factory=list)
    definition_sql: str | None = None
    comment: str | None = None

    @property
    def reference(self) -> str:
        return f"{self.schema_name}.{self.name}"


class DialectDDLParser:
    """Parse the shared governed DDL subset using a specific SQL dialect."""

    def __init__(self, *, dialect: str, sqlglot_name: str) -> None:
        self._dialect = dialect
        self._sqlglot_name = sqlglot_name

    def parse(
        self,
        *,
        data_source_id: str,
        ddl: str,
        default_schema: str = "public",
    ) -> DataSourceSnapshot:
        if not ddl.strip():
            raise DDLParseError("DDL must not be empty")
        if not default_schema.strip():
            raise DDLParseError("Default schema must not be empty")
        try:
            statements = sqlglot.parse(ddl, read=self._sqlglot_name)
        except ParseError as error:
            raise DDLParseError(f"Invalid {self._dialect} DDL: {error}") from error

        objects: dict[str, _ObjectBuilder] = {}
        relationships: list[RelationshipSnapshot] = []
        comments: list[exp.Comment] = []

        for statement in statements:
            if isinstance(statement, exp.Comment):
                comments.append(statement)
                continue
            if not isinstance(statement, exp.Create):
                raise DDLParseError(
                    f"Unsupported DDL statement: {type(statement).__name__}"
                )
            if statement.kind == "SCHEMA":
                continue
            if statement.kind == "TABLE":
                builder, table_relationships = self._parse_table(statement, default_schema)
                self._add_object(objects, builder)
                relationships.extend(table_relationships)
                continue
            if statement.kind == "VIEW":
                self._add_object(objects, self._parse_view(statement, default_schema))
                continue
            raise DDLParseError(f"Unsupported CREATE kind: {statement.kind or 'UNKNOWN'}")

        if not objects:
            raise DDLParseError("DDL contains no CREATE TABLE or CREATE VIEW statements")
        self._apply_comments(objects, comments, default_schema)

        return DataSourceSnapshot(
            data_source_id=data_source_id,
            dialect=self._dialect,
            objects=tuple(self._freeze_object(item) for item in objects.values()),
            relationships=tuple(relationships),
        )

    def _parse_table(
        self,
        statement: exp.Create,
        default_schema: str,
    ) -> tuple[_ObjectBuilder, list[RelationshipSnapshot]]:
        if not isinstance(statement.this, exp.Schema):
            raise DDLParseError("CREATE TABLE must include a column schema")
        table = self._table_target(statement.this.this, default_schema)
        builder = _ObjectBuilder(
            schema_name=table[0],
            name=table[1],
            kind=ObjectKind.TABLE,
        )
        properties = statement.args.get("properties")
        if isinstance(properties, exp.Properties):
            table_comment = next(
                (
                    item.this.this
                    for item in properties.expressions
                    if isinstance(item, exp.SchemaCommentProperty)
                    and isinstance(item.this, exp.Literal)
                    and isinstance(item.this.this, str)
                ),
                None,
            )
            if isinstance(table_comment, str):
                builder.comment = table_comment
        primary_keys: set[str] = set()
        relationships: list[RelationshipSnapshot] = []

        for item in statement.this.expressions:
            if isinstance(item, exp.ColumnDef):
                column, inline_relationship = self._parse_column(
                    item,
                    builder.reference,
                    default_schema,
                )
                builder.columns.append(column)
                if column.is_primary_key:
                    primary_keys.add(column.name)
                if inline_relationship is not None:
                    relationships.append(inline_relationship)
                continue
            constraint_name = item.name if isinstance(item, exp.Constraint) else ""
            expressions = item.expressions if isinstance(item, exp.Constraint) else [item]
            for constraint in expressions:
                if isinstance(constraint, exp.PrimaryKey):
                    primary_keys.update(column.name for column in constraint.expressions)
                elif isinstance(constraint, exp.ForeignKey):
                    relationships.append(
                        self._parse_foreign_key(
                            constraint,
                            constraint_name,
                            builder.reference,
                            default_schema,
                        )
                    )

        for column in builder.columns:
            if column.name in primary_keys:
                column.is_primary_key = True
                column.nullable = False
        if not builder.columns:
            raise DDLParseError(f"Table {builder.reference} has no columns")
        return builder, relationships

    def _parse_column(
        self,
        definition: exp.ColumnDef,
        source_object_ref: str,
        default_schema: str,
    ) -> tuple[_ColumnBuilder, RelationshipSnapshot | None]:
        if definition.kind is None:
            raise DDLParseError(f"Column {definition.name} has no physical type")
        column = _ColumnBuilder(
            name=definition.name,
            physical_type=definition.kind.sql(dialect=self._sqlglot_name),
            ordinal=0,
        )
        inline_relationship: RelationshipSnapshot | None = None
        for constraint in definition.constraints:
            kind = constraint.kind
            if isinstance(kind, exp.NotNullColumnConstraint):
                column.nullable = False
            elif isinstance(kind, exp.PrimaryKeyColumnConstraint):
                column.is_primary_key = True
                column.nullable = False
            elif isinstance(kind, exp.DefaultColumnConstraint):
                column.default_expression = kind.this.sql(dialect=self._sqlglot_name)
            elif isinstance(kind, exp.CommentColumnConstraint):
                if isinstance(kind.this, exp.Literal) and isinstance(kind.this.this, str):
                    column.comment = kind.this.this
            elif isinstance(kind, exp.Reference):
                target_ref, target_columns = self._reference_target(kind, default_schema)
                inline_relationship = RelationshipSnapshot(
                    name=f"{source_object_ref.replace('.', '_')}_{column.name}_fkey",
                    source_object_ref=source_object_ref,
                    target_object_ref=target_ref,
                    source_columns=(column.name,),
                    target_columns=target_columns,
                )
        return column, inline_relationship

    def _parse_foreign_key(
        self,
        foreign_key: exp.ForeignKey,
        constraint_name: str,
        source_object_ref: str,
        default_schema: str,
    ) -> RelationshipSnapshot:
        reference = foreign_key.args.get("reference")
        if not isinstance(reference, exp.Reference):
            raise DDLParseError("FOREIGN KEY has no REFERENCES target")
        source_columns = tuple(column.name for column in foreign_key.expressions)
        target_ref, target_columns = self._reference_target(reference, default_schema)
        name = constraint_name or (
            f"{source_object_ref.replace('.', '_')}_{'_'.join(source_columns)}_fkey"
        )
        return RelationshipSnapshot(
            name=name,
            source_object_ref=source_object_ref,
            target_object_ref=target_ref,
            source_columns=source_columns,
            target_columns=target_columns,
        )

    def _parse_view(self, statement: exp.Create, default_schema: str) -> _ObjectBuilder:
        target_expression = statement.this
        explicit_columns: tuple[str, ...] = ()
        if isinstance(target_expression, exp.Schema):
            explicit_columns = tuple(item.name for item in target_expression.expressions)
            target_expression = target_expression.this
        schema_name, object_name = self._table_target(target_expression, default_schema)
        if statement.expression is None:
            raise DDLParseError(f"View {schema_name}.{object_name} has no query definition")

        column_names = explicit_columns or self._view_projection_names(statement.expression)
        columns = [
            _ColumnBuilder(name=name, physical_type="unknown", ordinal=index)
            for index, name in enumerate(column_names, start=1)
        ]
        return _ObjectBuilder(
            schema_name=schema_name,
            name=object_name,
            kind=ObjectKind.VIEW,
            columns=columns,
            definition_sql=statement.expression.sql(dialect=self._sqlglot_name),
        )

    @staticmethod
    def _view_projection_names(query: exp.Expression) -> tuple[str, ...]:
        select = query if isinstance(query, exp.Select) else query.find(exp.Select)
        if select is None or any(projection.find(exp.Star) for projection in select.expressions):
            return ()
        names: list[str] = []
        for index, projection in enumerate(select.expressions, start=1):
            name = projection.alias_or_name or projection.output_name or projection.key
            names.append(name or f"column_{index}")
        return tuple(names)

    def _apply_comments(
        self,
        objects: dict[str, _ObjectBuilder],
        comments: list[exp.Comment],
        default_schema: str,
    ) -> None:
        for comment in comments:
            value = comment.expression.this if isinstance(comment.expression, exp.Literal) else None
            if not isinstance(value, str):
                continue
            comment_kind = comment.args.get("kind")
            if comment_kind == "TABLE" and isinstance(comment.this, exp.Table):
                schema_name, object_name = self._table_target(comment.this, default_schema)
                reference = f"{schema_name}.{object_name}"
                if reference not in objects:
                    raise DDLParseError(f"COMMENT references unknown table {reference}")
                objects[reference].comment = value
            elif comment_kind == "COLUMN" and isinstance(comment.this, exp.Column):
                schema_name = comment.this.db or default_schema
                reference = f"{schema_name}.{comment.this.table}"
                if reference not in objects:
                    raise DDLParseError(f"COMMENT references unknown table {reference}")
                column = next(
                    (item for item in objects[reference].columns if item.name == comment.this.name),
                    None,
                )
                if column is None:
                    raise DDLParseError(
                        f"COMMENT references unknown column {reference}.{comment.this.name}"
                    )
                column.comment = value

    def _reference_target(
        self,
        reference: exp.Reference,
        default_schema: str,
    ) -> tuple[str, tuple[str, ...]]:
        if not isinstance(reference.this, exp.Schema):
            raise DDLParseError("REFERENCES target must declare its columns")
        schema_name, object_name = self._table_target(
            reference.this.this,
            default_schema,
        )
        columns = tuple(column.name for column in reference.this.expressions)
        if not columns:
            raise DDLParseError("REFERENCES target must declare at least one column")
        return f"{schema_name}.{object_name}", columns

    @staticmethod
    def _table_target(
        expression: exp.Expression,
        default_schema: str,
    ) -> tuple[str, str]:
        if not isinstance(expression, exp.Table):
            raise DDLParseError("DDL target must be a table-like object")
        if expression.catalog:
            raise DDLParseError("Cross-database DDL targets are not supported")
        return expression.db or default_schema, expression.name

    @staticmethod
    def _add_object(objects: dict[str, _ObjectBuilder], builder: _ObjectBuilder) -> None:
        if builder.reference in objects:
            raise DDLParseError(f"Duplicate DDL object {builder.reference}")
        for index, column in enumerate(builder.columns, start=1):
            column.ordinal = index
        objects[builder.reference] = builder

    @staticmethod
    def _freeze_object(builder: _ObjectBuilder) -> SchemaObjectSnapshot:
        return SchemaObjectSnapshot(
            schema_name=builder.schema_name,
            name=builder.name,
            kind=builder.kind,
            columns=tuple(
                ColumnSnapshot(
                    name=column.name,
                    physical_type=column.physical_type,
                    ordinal=column.ordinal,
                    nullable=column.nullable,
                    default_expression=column.default_expression,
                    is_primary_key=column.is_primary_key,
                    comment=column.comment,
                )
                for column in builder.columns
            ),
            definition_sql=builder.definition_sql,
            comment=builder.comment,
        )


class PostgreSQLDDLParser(DialectDDLParser):
    def __init__(self) -> None:
        super().__init__(dialect="postgresql", sqlglot_name="postgres")


class MySQLDDLParser(DialectDDLParser):
    def __init__(self) -> None:
        super().__init__(dialect="mysql", sqlglot_name="mysql")


class MariaDBDDLParser(DialectDDLParser):
    def __init__(self) -> None:
        super().__init__(dialect="mariadb", sqlglot_name="mysql")


class OracleDDLParser(DialectDDLParser):
    def __init__(self) -> None:
        super().__init__(dialect="oracle", sqlglot_name="oracle")


class SQLServerDDLParser(DialectDDLParser):
    def __init__(self) -> None:
        super().__init__(dialect="sqlserver", sqlglot_name="tsql")
