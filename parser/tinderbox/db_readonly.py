"""Minimal read-only Postgres client mimicking the supabase-py calls Tinderbox's
MCP server uses (select/eq/like/in_/order/limit/execute, rpc).

Used only when TINDERBOX_DB_DSN is set (remote MCP bridge, role tinderbox_reader).
Writes raise. Identifiers are validated; values are always bound parameters.
"""
import re
import threading
from types import SimpleNamespace

import psycopg2
import psycopg2.extras

_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def _ident(name: str) -> str:
    if not _IDENT.match(name):
        raise ValueError(f"bad identifier {name!r}")
    return f'"{name}"'


class _Query:
    def __init__(self, client, schema, table):
        self._c, self._schema, self._table = client, schema, table
        self._cols, self._where, self._params = "*", [], []
        self._order, self._limit = [], None

    def select(self, cols="*"):
        self._cols = cols if cols == "*" else ",".join(_ident(c.strip()) for c in cols.split(","))
        return self

    def eq(self, col, val):
        self._where.append(f"{_ident(col)} = %s"); self._params.append(val); return self

    def like(self, col, pat):
        self._where.append(f"{_ident(col)} LIKE %s"); self._params.append(pat); return self

    def in_(self, col, vals):
        self._where.append(f"{_ident(col)} = ANY(%s)"); self._params.append(list(vals)); return self

    def order(self, col, desc=False):
        self._order.append(f"{_ident(col)} {'DESC' if desc else 'ASC'}"); return self

    def limit(self, n):
        self._limit = int(n); return self

    def _write(self, *a, **k):
        raise PermissionError("read-only Tinderbox client")

    insert = update = upsert = delete = _write

    def execute(self):
        sql = f"SELECT {self._cols} FROM {_ident(self._schema)}.{_ident(self._table)}"
        if self._where:
            sql += " WHERE " + " AND ".join(self._where)
        if self._order:
            sql += " ORDER BY " + ", ".join(self._order)
        if self._limit is not None:
            sql += f" LIMIT {self._limit}"
        return SimpleNamespace(data=self._c._fetch(sql, self._params))


class _Rpc:
    def __init__(self, client, schema, fn, params):
        self._c, self._schema, self._fn, self._p = client, schema, fn, params

    def execute(self):
        args = ", ".join(f"{_ident(k)} => %s" for k in self._p)
        vals = [str(v) if isinstance(v, list) else v for v in self._p.values()]
        sql = f"SELECT * FROM {_ident(self._schema)}.{_ident(self._fn)}({args})"
        return SimpleNamespace(data=self._c._fetch(sql, vals))


class _Schema:
    def __init__(self, client, schema):
        self._c, self._s = client, schema

    def table(self, name):
        return _Query(self._c, self._s, name)

    def rpc(self, fn, params):
        return _Rpc(self._c, self._s, fn, params)


class ReadOnlyClient:
    def __init__(self, dsn: str):
        self._dsn, self._conn, self._lock = dsn, None, threading.Lock()

    def schema(self, name):
        return _Schema(self, name)

    def _connect(self):
        conn = psycopg2.connect(self._dsn, connect_timeout=10, application_name="tinderbox-mcp-remote")
        conn.set_session(readonly=True, autocommit=True)
        return conn

    def _fetch(self, sql, params):
        with self._lock:
            for attempt in (0, 1):
                try:
                    if self._conn is None or self._conn.closed:
                        self._conn = self._connect()
                    with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                        cur.execute(sql, params)
                        rows = cur.fetchall()
                    return [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()} for r in rows]
                except (psycopg2.OperationalError, psycopg2.InterfaceError):
                    self._conn = None
                    if attempt:
                        raise
