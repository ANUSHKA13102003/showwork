import json

import duckdb
import pandas as pd

from app.config import DATA, LINES, META, QUALITY
from pipeline import categories, clean, fetch

QUARANTINE = DATA / "quarantine.parquet"
SOURCE = {
    "name": "Online Retail II",
    "publisher": "UCI Machine Learning Repository",
    "doi": "10.24432/C5CG6D",
    "licence": "CC BY 4.0",
    "kaggle_mirror": fetch.KAGGLE_REF,
}


def write_lines(df):
    con = duckdb.connect()
    con.register("src", df)
    con.execute(f"""
        COPY (
            SELECT
                CAST(row_number() OVER (ORDER BY invoice_ts, invoice, stock_code) AS INTEGER) AS line_id,
                invoice, kind, stock_code, description, category,
                CAST(quantity AS INTEGER) AS quantity,
                unit_price,
                CAST(CAST(amount_pence AS DOUBLE) / 100 AS DECIMAL(18, 2)) AS amount,
                invoice_ts,
                CAST(invoice_ts AS DATE) AS invoice_date,
                CAST(date_trunc('week', invoice_ts) AS DATE) AS week,
                CAST(date_trunc('month', invoice_ts) AS DATE) AS month,
                country, customer_id, is_guest, customer_type
            FROM src ORDER BY invoice_ts, invoice, stock_code
        ) TO '{LINES.as_posix()}' (FORMAT parquet, COMPRESSION zstd)
    """)
    con.close()


def write_meta():
    con = duckdb.connect()
    con.execute(f"CREATE VIEW lines AS SELECT * FROM read_parquet('{LINES.as_posix()}')")
    lo, hi = con.execute("SELECT MIN(invoice_date), MAX(invoice_date) FROM lines").fetchone()
    weeks = con.execute("""
        SELECT week, MIN(invoice_date), MAX(invoice_date), COUNT(DISTINCT invoice_date)
        FROM lines GROUP BY week ORDER BY week
    """).fetchall()
    countries = [r[0] for r in con.execute(
        "SELECT country FROM lines GROUP BY country ORDER BY SUM(amount) DESC").fetchall()]
    cats = [r[0] for r in con.execute(
        "SELECT category FROM lines GROUP BY category ORDER BY SUM(amount) DESC").fetchall()]
    n = con.execute("SELECT COUNT(*) FROM lines").fetchone()[0]
    con.close()
    week_list = []
    for w, first, last, days in weeks:
        complete = (w >= lo) and (last >= w + pd.Timedelta(days=4)) and (w + pd.Timedelta(days=6) <= hi)
        week_list.append({"week": w.isoformat(), "complete": bool(complete), "trading_days": int(days)})
    complete = [w["week"] for w in week_list if w["complete"]]
    meta = {
        "min_date": lo.isoformat(),
        "max_date": hi.isoformat(),
        "as_of": hi.isoformat(),
        "latest_complete_week": complete[-1],
        "weeks": week_list,
        "countries": countries,
        "categories": cats,
        "rows": int(n),
        "currency": "GBP",
        "source": SOURCE,
    }
    META.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def build(use_llm=True):
    fetch.fetch()
    raw = pd.read_parquet(fetch.RAW_PARQUET)
    good, bad, counts = clean.apply_contract(raw)
    good = clean.add_customer_type(good)

    products = (good[["stock_code", "description_canonical"]].drop_duplicates("stock_code")
                .rename(columns={"description_canonical": "description"}))
    cmap = categories.categorise(products, use_llm=use_llm)
    check = categories.consistency_check(cmap) if use_llm else None
    old = categories.CATEGORY_REPORT
    if check is None and old.exists():
        check = json.loads(old.read_text(encoding="utf-8")).get("llm_consistency_check")
    cat_report = categories.report(cmap, check)

    good["description"] = good["description_canonical"]
    good["category"] = good["stock_code"].map(cmap.set_index("stock_code")["category"]).fillna("Other")
    good["amount_pence"] = (good["quantity"] * good["unit_price"] * 100).round().astype("int64")
    write_lines(good)

    bad_out = bad[["invoice", "stock_code_raw", "description_raw", "quantity", "invoice_ts", "unit_price",
                   "customer_id", "country", "source_sheet", "reason"]]
    bad_out.to_parquet(QUARANTINE, index=False, compression="zstd")

    meta = write_meta()
    counts["customer_type_rows"] = good["customer_type"].value_counts().to_dict()
    counts["output"] = {"lines_parquet_rows": meta["rows"], "min_date": meta["min_date"], "max_date": meta["max_date"]}
    QUALITY.parent.mkdir(parents=True, exist_ok=True)
    QUALITY.write_text(json.dumps(counts, indent=2), encoding="utf-8")
    return counts, cat_report, meta


if __name__ == "__main__":
    q, c, m = build()
    print(json.dumps({"quality": q["quarantine_by_reason"], "stages": c["stages"]}, indent=2))
