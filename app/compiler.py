from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app import db

FILTER_DIMS = ("country", "category", "customer_type")
TIME_DIMS = ("week", "month")


class CompileError(ValueError):
    pass


class Filter(BaseModel):
    dimension: Literal["country", "category", "customer_type"]
    values: list[str] = Field(min_length=1, max_length=20)


class TimeRange(BaseModel):
    start: date
    end: date  # exclusive

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("time range end must be after start")
        return self


class Request(BaseModel):
    metric: str
    group_by: list[str] = Field(default_factory=list, max_length=2)
    filters: list[Filter] = Field(default_factory=list, max_length=3)
    time_range: TimeRange | None = None
    compare_to: Literal["previous_period", "previous_year"] | None = None
    sort: Literal["desc", "asc"] = "desc"
    limit: int = Field(default=20, ge=1, le=100)

    @field_validator("metric")
    @classmethod
    def known_metric(cls, v):
        if v not in db.semantic()["metrics"]:
            raise ValueError(f"unknown metric: {v}")
        return v

    @field_validator("group_by")
    @classmethod
    def known_dims(cls, v):
        dims = db.semantic()["dimensions"]
        for d in v:
            if d not in dims:
                raise ValueError(f"unknown dimension: {d}")
        if len(set(v)) != len(v):
            raise ValueError("duplicate dimension")
        return v

    @field_validator("filters")
    @classmethod
    def known_values(cls, v):
        allowed = allowed_values()
        out = []
        for f in v:
            lookup = {a.lower(): a for a in allowed[f.dimension]}
            vals = []
            for raw in f.values:
                canon = lookup.get(raw.strip().lower())
                if canon is None:
                    raise ValueError(f"unknown {f.dimension}: {raw}")
                vals.append(canon)
            out.append(Filter(dimension=f.dimension, values=vals))
        return out

    @field_validator("time_range")
    @classmethod
    def in_data(cls, v):
        if v is None:
            return v
        m = db.meta()
        lo, hi = date.fromisoformat(m["min_date"]), date.fromisoformat(m["max_date"]) + timedelta(days=1)
        if v.end <= lo or v.start >= hi:
            raise ValueError(f"the data covers {m['min_date']} to {m['max_date']}")
        return v


def allowed_values():
    m = db.meta()
    return {
        "country": m.get("countries", []),
        "category": m.get("categories", []),
        "customer_type": ["new", "returning", "guest"],
    }


def shift(tr, compare_to):
    if compare_to == "previous_year":
        return TimeRange(start=_year_back(tr.start), end=_year_back(tr.end))
    span = tr.end - tr.start
    if tr.start.day == 1 and tr.end.day == 1 and span.days >= 28:
        months = (tr.end.year - tr.start.year) * 12 + tr.end.month - tr.start.month
        return TimeRange(start=_months_back(tr.start, months), end=tr.start)
    return TimeRange(start=tr.start - span, end=tr.start)


def _year_back(d):
    try:
        return d.replace(year=d.year - 1)
    except ValueError:
        return d.replace(year=d.year - 1, day=28)


def _months_back(d, n):
    y, m = divmod(d.year * 12 + d.month - 1 - n, 12)
    return date(y, m + 1, 1)


def where_clause(req, time_range=None):
    tr = time_range if time_range is not None else req.time_range
    parts, params = [], []
    if tr is not None:
        parts.append("invoice_date >= ? AND invoice_date < ?")
        params += [tr.start, tr.end]
    dims = db.semantic()["dimensions"]
    for f in req.filters:
        col = dims[f.dimension]["sql"]
        marks = ", ".join("?" for _ in f.values)
        parts.append(f"{col} IN ({marks})")
        params += f.values
    return (" WHERE " + " AND ".join(parts)) if parts else "", params


def compile_sql(req, time_range=None):
    sem = db.semantic()
    metric = sem["metrics"][req.metric]
    dims = [(d, sem["dimensions"][d]["sql"]) for d in req.group_by]
    select = [f"{sql} AS {name}" for name, sql in dims] + [f"{metric['sql']} AS value"]
    where, params = where_clause(req, time_range)
    sql = f"SELECT {', '.join(select)} FROM {sem['table']}{where}"
    if dims:
        sql += " GROUP BY " + ", ".join(name for name, _ in dims)
        if any(name in TIME_DIMS for name, _ in dims):
            sql += " ORDER BY " + ", ".join(dims_order(dims))
        else:
            sql += f" ORDER BY value {req.sort.upper()} NULLS LAST, {dims[0][0]}"
        sql += f" LIMIT {int(req.limit)}"
    return sql, params


def dims_order(dims):
    return [name for name, _ in dims if name in TIME_DIMS] + [n for n, _ in dims if n not in TIME_DIMS]


def count_sql(req, time_range=None):
    where, params = where_clause(req, time_range)
    return f"SELECT COUNT(*) FROM {db.semantic()['table']}{where}", params


def display_sql(sql, params):
    out = sql
    for p in params:
        if isinstance(p, str):
            lit = "'" + p.replace("'", "''") + "'"
        elif isinstance(p, date):
            lit = f"DATE '{p.isoformat()}'"
        else:
            lit = str(p)
        out = out.replace("?", lit, 1)
    return out


def plain(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, date):
        return v.isoformat()
    return v


def execute(req, time_range=None):
    sql, params = compile_sql(req, time_range)
    cols, rows = db.query(sql, params)
    csql, cparams = count_sql(req, time_range)
    scanned = db.query(csql, cparams)[1][0][0]
    return {
        "columns": cols,
        "rows": [[plain(v) for v in r] for r in rows],
        "raw_rows": rows,
        "sql": display_sql(sql, params),
        "rows_scanned": int(scanned),
    }


def run(req):
    current = execute(req)
    out = {"request": req.model_dump(mode="json"), "current": current, "previous": None}
    if req.compare_to:
        if req.time_range is None:
            raise CompileError("a comparison needs a time range")
        prev_range = shift(req.time_range, req.compare_to)
        out["previous"] = execute(req, prev_range)
        out["previous_range"] = prev_range.model_dump(mode="json")
    return out


def parse(payload):
    try:
        return Request.model_validate(payload)
    except Exception as exc:  # pydantic ValidationError or bad types
        raise CompileError(str(exc)) from exc
