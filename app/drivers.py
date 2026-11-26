from decimal import Decimal

from app import compiler, db

DRIVER_DIMS = ("country", "category", "customer_type")


def breakdown(metric, current, previous, filters=(), dims=DRIVER_DIMS, top=5):
    """Split the change in an additive metric by each dimension. Each split sums exactly to the total."""
    sem = db.semantic()
    m = sem["metrics"][metric]
    if not m.get("additive") or "line_sql" not in m:
        raise compiler.CompileError(f"{m['label']} is not additive, so it cannot be split into drivers")
    req = compiler.Request(metric=metric, filters=list(filters))
    f_where, f_params = compiler.where_clause(req)
    f_where = f_where.replace(" WHERE ", " AND ", 1) if f_where else ""
    expr = m["line_sql"]
    total_sql = (
        f"SELECT SUM(CASE WHEN invoice_date >= ? AND invoice_date < ? THEN {expr} ELSE 0 END) AS current, "
        f"SUM(CASE WHEN invoice_date >= ? AND invoice_date < ? THEN {expr} ELSE 0 END) AS previous, "
        f"COUNT(*) AS rows_scanned FROM {sem['table']} "
        f"WHERE ((invoice_date >= ? AND invoice_date < ?) OR (invoice_date >= ? AND invoice_date < ?)){f_where}"
    )
    span = [current.start, current.end, previous.start, previous.end]
    t_params = span + span + f_params
    cur_total, prev_total, scanned = db.query(total_sql, t_params)[1][0]
    cur_total, prev_total = Decimal(cur_total or 0), Decimal(prev_total or 0)
    out = {
        "metric": metric,
        "current": float(cur_total),
        "previous": float(prev_total),
        "change": float(cur_total - prev_total),
        "change_pct": float((cur_total - prev_total) / prev_total) if prev_total else None,
        "rows_scanned": int(scanned),
        "total_sql": compiler.display_sql(total_sql, t_params),
        "dimensions": {},
    }
    for dim in dims:
        col = sem["dimensions"][dim]["sql"]
        sql = (
            f"SELECT {col} AS member, "
            f"SUM(CASE WHEN invoice_date >= ? AND invoice_date < ? THEN {expr} ELSE 0 END) AS current, "
            f"SUM(CASE WHEN invoice_date >= ? AND invoice_date < ? THEN {expr} ELSE 0 END) AS previous "
            f"FROM {sem['table']} "
            f"WHERE ((invoice_date >= ? AND invoice_date < ?) OR (invoice_date >= ? AND invoice_date < ?)){f_where} "
            f"GROUP BY {col}"
        )
        rows = db.query(sql, t_params)[1]
        items = [(r[0], Decimal(r[1] or 0), Decimal(r[2] or 0)) for r in rows]
        items.sort(key=lambda r: (-abs(r[1] - r[2]), str(r[0])))
        shown, rest = items[:top], items[top:]
        members = [{"member": str(k), "current": float(c), "previous": float(p), "change": float(c - p)}
                   for k, c, p in shown]
        if rest:
            rc = sum((r[1] for r in rest), Decimal(0))
            rp = sum((r[2] for r in rest), Decimal(0))
            members.append({"member": f"All other ({len(rest)})", "current": float(rc), "previous": float(rp),
                            "change": float(rc - rp), "other": True})
        exact = sum((c - p for _, c, p in items), Decimal(0))
        out["dimensions"][dim] = {
            "members": members,
            "sum_of_changes": float(exact),
            "sums_to_total": exact == cur_total - prev_total,
            "sql": compiler.display_sql(sql, t_params),
        }
    return out
