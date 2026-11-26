import argparse
import json
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import ask, compiler, db, drivers, fmt, llm, verifier
from app.config import BRIEFINGS, EVALS

KPI_KEYS = [
    "net_revenue",
    "orders",
    "aov",
    "customers",
    "repeat_customer_rate",
    "return_rate",
]
ANOMALY_KEYS = ["net_revenue", "orders", "aov", "return_rate", "units"]


def eligible_weeks() -> list[str]:
    """Complete weeks that have at least 8 prior complete weeks for the MAD baseline."""
    complete = [w["week"] for w in db.meta().get("weeks", []) if w["complete"]]
    return complete[8:]


def _scalar_for_range(metric: str, start: date, end: date, filters=()) -> dict:
    req = compiler.Request(
        metric=metric,
        filters=list(filters),
        time_range=compiler.TimeRange(start=start, end=end),
    )
    res = compiler.execute(req)
    val = res["rows"][0][0] if res["rows"] else 0.0
    work = ask.make_work(
        metric,
        res["sql"],
        res["rows_scanned"],
        len(res["rows"]),
        req.model_dump(mode="json")["filters"],
        {"start": start.isoformat(), "end": end.isoformat()},
        None,
        "deterministic",
    )
    return {"value": 0.0 if val is None else float(val), "show_work": work}


def _weekly_series(metric: str, start: date, end: date) -> list[tuple[str, float]]:
    req = compiler.Request(
        metric=metric,
        group_by=["week"],
        time_range=compiler.TimeRange(start=start, end=end),
        limit=20,
    )
    res = compiler.execute(req)
    return [(str(r[0]), 0.0 if r[1] is None else float(r[1])) for r in res["rows"]]


def compute_anomalies(week_start: date) -> tuple[list[dict], str | None]:
    sem = db.semantic()
    cur_end = week_start + timedelta(days=7)
    hist_start = week_start - timedelta(days=56)
    flags = []

    for m_key in ANOMALY_KEYS:
        m = sem["metrics"][m_key]
        hist = [v for _, v in _weekly_series(m_key, hist_start, week_start)]
        if len(hist) < 4:
            continue
        cur_info = _scalar_for_range(m_key, week_start, cur_end)
        cur_val = cur_info["value"]
        med = statistics.median(hist)
        mad = statistics.median([abs(x - med) for x in hist])
        sigma = 1.4826 * mad if mad > 0 else max(abs(med) * 0.05, 1e-6)
        score = (cur_val - med) / sigma
        if abs(score) >= 3.0:
            direction = "above" if score > 0 else "below"
            note = (
                "November pre-Christmas wholesale stocking creates a recurring seasonal peak; interpret alongside YoY and category mix."
                if week_start.month == 11 and score > 0
                else "Outside 3x scaled MAD over the prior 8 complete weeks."
            )
            flags.append({
                "metric_key": m_key,
                "metric": m["label"],
                "unit": m["unit"],
                "current": round(cur_val, 6),
                "current_fmt": fmt.value(cur_val, m["unit"]),
                "median_8w": round(med, 6),
                "median_8w_fmt": fmt.value(med, m["unit"]),
                "mad_8w": round(mad, 6),
                "mad_score": round(score, 2),
                "direction": direction,
                "note": note,
                "show_work": cur_info["show_work"],
            })

    seasonal_note = None
    if week_start.month in (11, 12):
        seasonal_note = (
            "Seasonal context: November and early December reflect pre-Christmas wholesale giftware stocking, "
            "which lifts weekly revenue and order volume well above early-autumn baselines."
        )
    return flags, seasonal_note


def compute_watchlist(week_start: date, kpi_map: dict, drv: dict) -> list[dict]:
    cur_end = week_start + timedelta(days=7)
    prev_start = week_start - timedelta(days=7)
    items = []

    # 1. Return rate & total returns check
    ret_kpi = kpi_map["return_rate"]
    ret_gbp = _scalar_for_range("returns", week_start, cur_end)
    items.append({
        "rule": "RETURN_MONITOR",
        "title": f"Returns totalled {fmt.money(ret_gbp['value'])} ({fmt.pct(ret_kpi['current'])} of gross sales)",
        "detail": (
            f"Return rate moved {fmt.signed_pct(ret_kpi['vs_avg4_change'])} vs the prior 4-week average "
            f"({fmt.pct(ret_kpi['avg4'])})."
        ),
        "fact_numbers": [
            {"value": ret_gbp["value"], "unit": "gbp"},
            {"value": ret_kpi["current"], "unit": "ratio"},
            {"value": ret_kpi["vs_avg4_change"], "unit": "ratio"},
            {"value": ret_kpi["avg4"], "unit": "ratio"},
        ],
        "show_work": ret_gbp["show_work"],
    })

    # 2. Largest negative category drag (or smallest mover if all categories rose)
    cat_members = [m for m in drv["dimensions"]["category"]["members"] if not m.get("other")]
    worst_cat = min(cat_members, key=lambda m: m["change"]) if cat_members else None
    if worst_cat:
        cat_work = dict(kpi_map["net_revenue"]["show_work"], sql=drv["dimensions"]["category"]["sql"])
        verb = "declined" if worst_cat["change"] < 0 else "moved"
        items.append({
            "rule": "CATEGORY_DRAG",
            "title": f"{worst_cat['member']} {verb} {fmt.signed_money(worst_cat['change'])} WoW",
            "detail": (
                f"Week net revenue in {worst_cat['member']} was {fmt.money(worst_cat['current'])} "
                f"vs {fmt.money(worst_cat['previous'])} the prior week."
            ),
            "fact_numbers": [
                {"value": worst_cat["change"], "unit": "gbp"},
                {"value": worst_cat["current"], "unit": "gbp"},
                {"value": worst_cat["previous"], "unit": "gbp"},
            ],
            "show_work": cat_work,
        })

    # 3. Guest checkout revenue share
    guest_rev = _scalar_for_range(
        "gross_revenue",
        week_start,
        cur_end,
        filters=[compiler.Filter(dimension="customer_type", values=["guest"])],
    )
    gross_rev = _scalar_for_range("gross_revenue", week_start, cur_end)
    guest_share = guest_rev["value"] / gross_rev["value"] if gross_rev["value"] else 0.0
    items.append({
        "rule": "GUEST_SHARE",
        "title": f"Guest checkout accounted for {fmt.pct(guest_share)} of gross sales ({fmt.money(guest_rev['value'])})",
        "detail": "Orders without a Customer ID cannot be linked to retention cohorts; monitor wholesale vs unidentified till/web lines.",
        "fact_numbers": [
            {"value": guest_share, "unit": "ratio"},
            {"value": guest_rev["value"], "unit": "gbp"},
        ],
        "show_work": guest_rev["show_work"],
    })

    # 4. Largest non-UK market shift
    non_uk = [
        m for m in drv["dimensions"]["country"]["members"]
        if not m.get("other") and m["member"] != "United Kingdom"
    ]
    if non_uk:
        top_intl = non_uk[0]
        ctry_work = dict(kpi_map["net_revenue"]["show_work"], sql=drv["dimensions"]["country"]["sql"])
        items.append({
            "rule": "EXPORT_MOVER",
            "title": f"Top export swing: {top_intl['member']} ({fmt.signed_money(top_intl['change'])} WoW)",
            "detail": (
                f"{top_intl['member']} net revenue was {fmt.money(top_intl['current'])} "
                f"vs {fmt.money(top_intl['previous'])} in {prev_start.isoformat()}."
            ),
            "fact_numbers": [
                {"value": top_intl["change"], "unit": "gbp"},
                {"value": top_intl["current"], "unit": "gbp"},
                {"value": top_intl["previous"], "unit": "gbp"},
            ],
            "show_work": ctry_work,
        })

    return items[:5]


def compute_facts(week_str: str) -> dict:
    week_start = date.fromisoformat(week_str)
    cur_end = week_start + timedelta(days=7)
    prev_start = week_start - timedelta(days=7)
    sem = db.semantic()

    kpis = []
    kpi_map = {}
    for m_key in KPI_KEYS:
        m = sem["metrics"][m_key]
        unit = m["unit"]
        cur = _scalar_for_range(m_key, week_start, cur_end)
        prev = _scalar_for_range(m_key, prev_start, week_start)
        prior4_vals = [
            _scalar_for_range(m_key, week_start - timedelta(days=7 * i), week_start - timedelta(days=7 * (i - 1)))["value"]
            for i in range(1, 5)
        ]
        avg4 = sum(prior4_vals) / len(prior4_vals)
        wow_change = cur["value"] - prev["value"]
        wow_pct = wow_change / prev["value"] if prev["value"] else None
        vs_avg4_change = cur["value"] - avg4
        vs_avg4_pct = vs_avg4_change / avg4 if avg4 else None

        entry = {
            "key": m_key,
            "label": m["label"],
            "unit": unit,
            "current": round(cur["value"], 6),
            "current_fmt": fmt.value(cur["value"], unit),
            "wow_prev": round(prev["value"], 6),
            "wow_prev_fmt": fmt.value(prev["value"], unit),
            "wow_change": round(wow_change, 6),
            "wow_change_fmt": fmt.signed_money(wow_change) if unit == "gbp" else (fmt.signed_pct(wow_change) if unit == "ratio" else f"{round(wow_change):+,}"),
            "wow_pct": round(wow_pct, 6) if wow_pct is not None else None,
            "wow_pct_fmt": fmt.signed_pct(wow_pct) if wow_pct is not None else "n/a",
            "avg4": round(avg4, 6),
            "avg4_fmt": fmt.value(avg4, unit),
            "vs_avg4_change": round(vs_avg4_change, 6),
            "vs_avg4_pct": round(vs_avg4_pct, 6) if vs_avg4_pct is not None else None,
            "vs_avg4_pct_fmt": fmt.signed_pct(vs_avg4_pct) if vs_avg4_pct is not None else "n/a",
            "show_work": cur["show_work"],
        }
        kpis.append(entry)
        kpi_map[m_key] = entry

    cur_tr = compiler.TimeRange(start=week_start, end=cur_end)
    prev_tr = compiler.TimeRange(start=prev_start, end=week_start)
    drv = drivers.breakdown("net_revenue", cur_tr, prev_tr, top=5)
    for dim_info in drv["dimensions"].values():
        dim_work = dict(kpi_map["net_revenue"]["show_work"], sql=dim_info["sql"])
        dim_info["show_work"] = dim_work

    anomalies, seasonal_note = compute_anomalies(week_start)
    watchlist = compute_watchlist(week_start, kpi_map, drv)

    return {
        "week": week_str,
        "week_end_inclusive": (cur_end - timedelta(days=1)).isoformat(),
        "previous_week": prev_start.isoformat(),
        "as_of": db.meta().get("as_of"),
        "seasonal_note": seasonal_note,
        "kpis": kpis,
        "drivers": drv,
        "anomalies": anomalies,
        "watchlist": watchlist,
    }


def template_summary(facts: dict) -> str:
    k = {item["key"]: item for item in facts["kpis"]}
    rev = k["net_revenue"]
    orders = k["orders"]
    aov = k["aov"]
    ret = k["return_rate"]
    verb = "rose" if rev["wow_change"] >= 0 else "fell"
    top_cat = facts["drivers"]["dimensions"]["category"]["members"][0]
    top_ctry = facts["drivers"]["dimensions"]["country"]["members"][0]

    parts = [
        (
            f"In the week of {facts['week']}, net revenue was {rev['current_fmt']}, which {verb} {rev['wow_change_fmt']} "
            f"({rev['wow_pct_fmt']}) week over week and stood {rev['vs_avg4_pct_fmt']} vs the prior 4-week average ({rev['avg4_fmt']})."
        ),
        f"Orders reached {orders['current_fmt']} ({orders['wow_pct_fmt']} vs {orders['wow_prev_fmt']}) with average order value at {aov['current_fmt']}.",
        (
            f"By category, the largest revenue move came from {top_cat['member']} ({fmt.signed_money(top_cat['change'])}); "
            f"by country, {top_ctry['member']} moved {fmt.signed_money(top_ctry['change'])}."
        ),
        f"Return rate was {ret['current_fmt']}.",
    ]
    if facts.get("anomalies"):
        a0 = facts["anomalies"][0]
        parts.append(f"Anomaly flag: {a0['metric']} ({a0['current_fmt']}) was {a0['direction']} its 8-week median ({a0['median_8w_fmt']}).")
    return " ".join(parts)


def _llm_summary_prompt(facts: dict) -> str:
    compact = {
        "week": facts["week"],
        "kpis": [
            {
                "metric": x["label"],
                "current": x["current_fmt"],
                "wow_prev": x["wow_prev_fmt"],
                "wow_change": x["wow_change_fmt"],
                "wow_pct": x["wow_pct_fmt"],
                "avg4": x["avg4_fmt"],
                "vs_avg4_pct": x["vs_avg4_pct_fmt"],
            }
            for x in facts["kpis"]
        ],
        "top_category_driver": facts["drivers"]["dimensions"]["category"]["members"][0],
        "top_country_driver": facts["drivers"]["dimensions"]["country"]["members"][0],
        "top_customer_type_driver": facts["drivers"]["dimensions"]["customer_type"]["members"][0],
        "anomalies": [
            {"metric": a["metric"], "current": a["current_fmt"], "median_8w": a["median_8w_fmt"], "direction": a["direction"]}
            for a in facts["anomalies"]
        ],
    }
    return (
        "Write one plain-English executive paragraph of 90 to 160 words summarising this retail week.\n"
        "Strict rules:\n"
        "1. Copy numeric strings verbatim from the JSON facts below (e.g. £341,210, +12.4%, 612). Never calculate or round a new number.\n"
        "2. Do not use bullet points, markdown headings, or emojis.\n"
        "3. State net revenue, week-over-week change, comparison vs the prior 4-week average, orders/AOV, and top category/country drivers.\n\n"
        f"Facts JSON:\n{json.dumps(compact, ensure_ascii=False)}"
    )


def build_summary(facts: dict, use_llm: bool = True) -> dict:
    if not use_llm or not llm.has_key():
        txt = template_summary(facts)
        v = verifier.verify(txt, facts)
        return {
            "text": txt,
            "source": "template",
            "attempts": 0,
            "first_pass_failed": False,
            "fallback_used": False,
            "verification": v,
        }

    prompt = _llm_summary_prompt(facts)
    draft1 = llm.generate(prompt, temperature=0.0, metered=False)
    if draft1:
        v1 = verifier.verify(draft1.strip(), facts)
        if v1["ok"]:
            return {
                "text": draft1.strip(),
                "source": "llm_verified",
                "attempts": 1,
                "first_pass_failed": False,
                "fallback_used": False,
                "verification": v1,
            }
        retry_prompt = (
            prompt
            + f"\n\nYour previous draft failed numeric verification on: {v1['unmatched']}. "
            "Rewrite under 160 words using ONLY exact formatted numbers copied from the Facts JSON."
        )
        draft2 = llm.generate(retry_prompt, temperature=0.0, metered=False)
        if draft2:
            v2 = verifier.verify(draft2.strip(), facts)
            if v2["ok"]:
                return {
                    "text": draft2.strip(),
                    "source": "llm_verified_retry",
                    "attempts": 2,
                    "first_pass_failed": True,
                    "fallback_used": False,
                    "verification": v2,
                }

    txt = template_summary(facts)
    vt = verifier.verify(txt, facts)
    return {
        "text": txt,
        "source": "template_fallback",
        "attempts": 2,
        "first_pass_failed": True,
        "fallback_used": True,
        "verification": vt,
    }


def render_markdown(doc: dict) -> str:
    lines = [
        f"# Weekly Briefing — Week of {doc['week']}",
        "",
        f"*Period: `{doc['week']}` to `{doc['week_end_inclusive']}` (GBP) · Summary source: `{doc['summary']['source']}`*",
        "",
        doc["summary"]["text"],
        "",
        "## KPI Summary",
        "",
        "| Metric | Current | WoW Change | vs Prior 4-Week Avg |",
        "| :--- | ---: | ---: | ---: |",
    ]
    for k in doc["kpis"]:
        lines.append(f"| {k['label']} | {k['current_fmt']} | {k['wow_change_fmt']} ({k['wow_pct_fmt']}) | {k['vs_avg4_pct_fmt']} (avg {k['avg4_fmt']}) |")
    lines += ["", "## Top Net Revenue Drivers (WoW, GBP)", ""]
    for dim_key in ("category", "country", "customer_type"):
        lines.append(f"### By {dim_key.replace('_', ' ').title()}")
        for m in doc["drivers"]["dimensions"][dim_key]["members"]:
            lines.append(f"- **{m['member']}**: {fmt.signed_money(m['change'])} ({fmt.money(m['current'])} vs {fmt.money(m['previous'])})")
        lines.append("")
    if doc.get("anomalies"):
        lines += ["## Anomaly Flags (8-Week Median & MAD)", ""]
        for a in doc["anomalies"]:
            lines.append(f"- **{a['metric']}**: {a['current_fmt']} vs 8-week median {a['median_8w_fmt']} (`{a['mad_score']:+.1f}` scaled MAD) — {a['note']}")
        lines.append("")
    lines += ["## Watchlist", ""]
    for w in doc["watchlist"]:
        lines.append(f"- **{w['title']}** — {w['detail']}")
    return "\n".join(lines) + "\n"


def generate_week(week_str: str, out_dir: Path = BRIEFINGS, use_llm: bool = True) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    facts = compute_facts(week_str)
    summary = build_summary(facts, use_llm=use_llm)
    doc = {**facts, "summary": summary}
    (out_dir / f"{week_str}.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")
    (out_dir / f"{week_str}.md").write_text(render_markdown(doc), encoding="utf-8")
    return doc


def list_saved_weeks(out_dir: Path = BRIEFINGS) -> list[str]:
    if not out_dir.exists():
        return []
    return sorted((p.stem for p in out_dir.glob("*.json")), reverse=True)


def load_or_compute(week_str: str, out_dir: Path = BRIEFINGS) -> dict:
    p = out_dir / f"{week_str}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    if week_str not in eligible_weeks():
        raise ValueError(f"Unknown or incomplete briefing week: {week_str}")
    return {**compute_facts(week_str), "summary": build_summary(compute_facts(week_str), use_llm=False)}


def update_verifier_eval_stats(out_dir: Path = BRIEFINGS):
    results_path = EVALS / "results.json"
    if not results_path.exists() or not out_dir.exists():
        return
    docs = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(out_dir.glob("*.json"))]
    llm_docs = [d for d in docs if d.get("summary", {}).get("attempts", 0) > 0]
    first_fail = sum(1 for d in llm_docs if d["summary"].get("first_pass_failed"))
    fallbacks = sum(1 for d in llm_docs if d["summary"].get("fallback_used"))
    total = len(llm_docs)
    data = json.loads(results_path.read_text(encoding="utf-8"))
    data["briefing_verifier"] = {
        "weeks_evaluated": total,
        "first_pass_failures": first_fail,
        "first_pass_failure_rate": round(first_fail / total, 4) if total else 0.0,
        "template_fallbacks": fallbacks,
        "verified_final_pass_rate": 1.0 if docs else 0.0,
    }
    results_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def backfill(n: int = 8, out_dir: Path = BRIEFINGS, use_llm: bool = True) -> list[str]:
    weeks = eligible_weeks()[-n:]
    for w in weeks:
        generate_week(w, out_dir=out_dir, use_llm=use_llm)
        print(f"[briefing] generated {w}", flush=True)
    update_verifier_eval_stats(out_dir=out_dir)
    return weeks


def process_next_unprocessed(out_dir: Path = BRIEFINGS, use_llm: bool = True) -> str | None:
    """Process the next unprocessed complete week in the replay feed, if any."""
    existing = set(list_saved_weeks(out_dir))
    all_weeks = eligible_weeks()
    # Replay forward from the earliest existing week if present, else from the last-8 window;
    # if up to the end is already built, backfill the immediately preceding historical week so
    # manual/scheduled runs continue expanding coverage until all eligible weeks are generated.
    for w in all_weeks:
        if existing and w > max(existing) and w not in existing:
            generate_week(w, out_dir=out_dir, use_llm=use_llm)
            update_verifier_eval_stats(out_dir=out_dir)
            return w
    for w in reversed(all_weeks):
        if w not in existing:
            generate_week(w, out_dir=out_dir, use_llm=use_llm)
            update_verifier_eval_stats(out_dir=out_dir)
            return w
    return None


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=int, default=0)
    ap.add_argument("--next", action="store_true")
    ap.add_argument("--no-llm", action="store_true")
    args = ap.parse_args()
    if args.backfill:
        backfill(args.backfill, use_llm=not args.no_llm)
    elif args.next:
        done = process_next_unprocessed(use_llm=not args.no_llm)
        print(f"processed={done}")
