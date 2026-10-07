"""
In-memory stand-in for the supabase-py query builder, covering exactly the
calls the ETF pollers, the AI pipeline and the bot make. Not a test itself
(run_all.sh only runs tests/test_*.py).
"""
import copy
import itertools

_ids = itertools.count(1)


def _cmp(x, v):
    """-1/0/1, numerically when both sides are numbers, else as strings (ISO dates)."""
    try:
        a, b = float(x), float(v)
    except (TypeError, ValueError):
        a, b = str(x), str(v)
    return (a > b) - (a < b)


def _get(row, col):
    if "->>" in col:
        base, key = col.split("->>", 1)
        v = (row.get(base) or {}).get(key)
        return None if v is None else str(v)
    return row.get(col)


class _Res:
    def __init__(self, data, count=None):
        self.data, self.count = data, count


class _Q:
    def __init__(self, db, table):
        self.db, self.t = db, table
        self.op, self.payload, self.filters = "select", None, []
        self.on_conflict, self.ignore_dup = None, False
        self._order, self._desc, self._range, self._limit = None, False, None, None

    # builders
    def select(self, *a, **k):
        self.op = "select"; return self

    def insert(self, rows):
        self.op, self.payload = "insert", rows; return self

    def upsert(self, rows, on_conflict=None, ignore_duplicates=False, **k):
        self.op, self.payload = "upsert", rows
        self.on_conflict, self.ignore_dup = on_conflict, ignore_duplicates
        return self

    def update(self, values):
        self.op, self.payload = "update", values; return self

    def delete(self):
        self.op = "delete"; return self

    def eq(self, c, v):
        self.filters.append(("eq", c, v)); return self

    def in_(self, c, v):
        self.filters.append(("in", c, list(v))); return self

    def gte(self, c, v):
        self.filters.append(("gte", c, v)); return self

    def lt(self, c, v):
        self.filters.append(("lt", c, v)); return self

    def ilike(self, c, pattern):
        self.filters.append(("ilike", c, pattern)); return self

    def order(self, c, desc=False):
        self._order, self._desc = c, desc; return self

    def range(self, a, b):
        self._range = (a, b); return self

    def limit(self, n):
        self._limit = n; return self

    # evaluation
    def _match(self, row):
        for op, c, v in self.filters:
            x = _get(row, c)
            if op == "eq" and str(x) != str(v):
                return False
            if op == "in" and x not in v and str(x) not in [str(i) for i in v]:
                return False
            if op == "gte" and (x is None or _cmp(x, v) < 0):
                return False
            if op == "lt" and (x is None or _cmp(x, v) >= 0):
                return False
            if op == "ilike":
                pre = v.rstrip("%").lower()
                if not str(x or "").lower().startswith(pre):
                    return False
        return True

    def _key(self, row):
        return tuple(row.get(c.strip()) for c in self.on_conflict.split(","))

    def execute(self):
        if self.db.fail_tables and self.t in self.db.fail_tables:
            raise RuntimeError(f"simulated failure on {self.t}")
        rows = self.db.tables.setdefault(self.t, [])
        if self.op == "select":
            out = [r for r in rows if self._match(r)]
            if self._order:
                out.sort(key=lambda r: (r.get(self._order) is None, str(r.get(self._order))),
                         reverse=self._desc)
            if self._range:
                a, b = self._range
                out = out[a:b + 1]
            if self._limit is not None:
                out = out[:self._limit]
            return _Res(copy.deepcopy(out), count=len(out))
        if self.op == "insert":
            new = self.payload if isinstance(self.payload, list) else [self.payload]
            for r in new:
                r = copy.deepcopy(r)
                r.setdefault("id", next(_ids))
                r.setdefault("created_at", "2026-10-02T12:00:00+00:00")
                rows.append(r)
            return _Res(new)
        if self.op == "upsert":
            new = self.payload if isinstance(self.payload, list) else [self.payload]
            for r in new:
                r = copy.deepcopy(r)
                hit = next((x for x in rows if self.on_conflict and self._key(x) == self._key(r)), None)
                if hit is not None:
                    if not self.ignore_dup:
                        hit.update(r)
                else:
                    r.setdefault("id", next(_ids))
                    rows.append(r)
            return _Res(new)
        if self.op == "update":
            hit = [r for r in rows if self._match(r)]
            for r in hit:
                r.update(copy.deepcopy(self.payload))
            return _Res(copy.deepcopy(hit))
        if self.op == "delete":
            keep = [r for r in rows if not self._match(r)]
            gone = len(rows) - len(keep)
            self.db.tables[self.t] = keep
            return _Res([{}] * gone)
        raise ValueError(self.op)


class FakeDB:
    def __init__(self):
        self.tables = {}
        self.fail_tables = set()

    def table(self, name):
        return _Q(self, name)

    def rows(self, name):
        return self.tables.get(name, [])
