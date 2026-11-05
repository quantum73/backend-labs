import contextlib
import dataclasses

from sqlalchemy import Engine, event


@dataclasses.dataclass
class _QueriesInfo:
    statements: list[str] = dataclasses.field(default_factory=list)


@contextlib.contextmanager
def db_queries_count():
    queries_info = _QueriesInfo()

    def _count(conn, cursor, statement, parameters, context, executemany) -> None:
        queries_info.statements.append(statement)

    event.listen(Engine, "before_cursor_execute", _count)
    try:
        yield queries_info
    finally:
        event.remove(Engine, "before_cursor_execute", _count)
