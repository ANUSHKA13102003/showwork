import argparse
import json
import math
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import ask, llm
from app.config import EVALS, LINES

QUESTIONS_YML = EVALS / "questions.yml"
BASELINE_CACHE = EVALS / "baseline_cache.json"
LLM_PLAN_CACHE = EVALS / "llm_cache.json"
RESULTS_JSON = EVALS / "results.json"


def wilson_ci(passed: int, total: int, z: float = 1.959964):
    if total == 0:
        return {"passed": 0, "total": 0, "rate": 0.0, "ci_low": 0.0, "ci_high": 0.0}
    p = passed / total
    denom = 1.0 + (z * z) / total
    center = (p + (z * z) / (2.0 * total)) / denom
    half = (z * math.sqrt((p * (1.0 - p)) / total + (z * z) / (4.0 * total * total))) / denom
    return {
        "passed": int(passed),
        "total": int(total),
        "rate": round(p, 4),
        "ci_low": round(max(0.0, center - half), 4),
        "ci_high": round(min(1.0, center + half), 4),
    }


def _shift_range(tr: dict, compare_to: str) -> dict:
    s = date.fromisoformat(tr["start"])
    e = date.fromisoformat(tr["end"])
    if compare_to == "previous_year":
        return {"start": s.replace(year=s.year - 1).isoformat(), "end": e.replace(year=e.year - 1).isoformat()}
    span = e - s
    if s.day == 1 and e.day == 1 and span.days >= 28:
        months = (e.year - s.year) * 12 + e.month - s.month
        y, m = divmod(s.year * 12 + s.month - 1 - months, 12)
        return {"start": date(y, m + 1, 1).isoformat(), "end": s.isoformat()}
    return {"start": (s - span).isoformat(), "end": s.isoformat()}


def _pandas_metric(sub: pd.DataFrame, metric: str) -> float:
    amt = sub["amount"].astype(float)
    is_sale = sub["kind"] == "sale"
    is_ret = sub["kind"] == "return"
    if metric == "net_revenue":
        return float(amt.sum())
    if metric == "gross_revenue":
        return float(amt[is_sale].sum())
    if metric == "returns":
        return float(-amt[is_ret].sum())
    if metric == "orders":
        return float(sub.loc[is_sale, "invoice"].nunique())
    if metric == "units":
        return float(sub.loc[is_sale, "quantity"].sum())
    if metric == "customers":
        valid = is_sale & (~sub["is_guest"])
        return float(sub.loc[valid, "customer_id"].nunique())
    if metric == "aov":
        orders = sub.loc[is_sale, "invoice"].nunique()
        return float(amt[is_sale].sum() / orders) if orders else 0.0
    if metric == "repeat_customer_rate":
        valid = is_sale & (~sub["is_guest"])
        denom = sub.loc[valid, "customer_id"].nunique()
        numer = sub.loc[valid & (sub["customer_type"] == "returning"), "customer_id"].nunique()
        return float(numer / denom) if denom else 0.0
    if metric == "return_rate":
        gross = amt[is_sale].sum()
        ret = -amt[is_ret].sum()
        return float(ret / gross) if gross else 0.0
    raise ValueError(f"Unsupported metric {metric}")


def _filter_df(df: pd.DataFrame, tr: dict | None, filters: list[dict]) -> pd.DataFrame:
    mask = pd.Series(True, index=df.index)
    if tr:
        s = date.fromisoformat(tr["start"])
        e = date.fromisoformat(tr["end"])
        dcol = pd.to_datetime(df["invoice_date"]).dt.date
        mask &= (dcol >= s) & (dcol < e)
    for f in filters:
        mask &= df[f["dimension"]].isin(f["values"])
    return df[mask]


def pandas_ground_truth(df: pd.DataFrame, expected: dict) -> float:
    """Independent ground truth using plain pandas DataFrame operations only."""
    metric = expected["metric"]
    tr = expected.get("time_range")
    filters = expected.get("filters", [])
    cur_df = _filter_df(df, tr, filters)

    if expected["kind"] == "why":
        prev_tr = _shift_range(tr, expected.get("compare_to") or "previous_period")
        prev_df = _filter_df(df, prev_tr, filters)
        return _pandas_metric(cur_df, metric) - _pandas_metric(prev_df, metric)

    group_by = expected.get("group_by", [])
    if not group_by:
        return _pandas_metric(cur_df, metric)

    g_col = group_by[0]
    vals = []
    for key, grp in cur_df.groupby(g_col, dropna=False):
        vals.append((str(key), _pandas_metric(grp, metric)))
    if g_col in ("week", "month"):
        vals.sort(key=lambda x: x[0])
    else:
        rev = expected.get("sort", "desc") != "asc"
        vals.sort(key=lambda x: x[1] if not rev else -x[1])
    return vals[0][1] if vals else 0.0


def _norm_filters(flist: list[dict]) -> list[tuple[str, tuple[str, ...]]]:
    return sorted((f["dimension"], tuple(sorted(f["values"]))) for f in (flist or []))


def request_matches(actual_resp: dict, expected: dict) -> bool:
    if actual_resp.get("status") != "answered":
        return False
    if actual_resp.get("kind") != expected["kind"]:
        return False
    req = actual_resp.get("request") or {}
    if req.get("metric") != expected["metric"]:
        return False
    if (req.get("group_by") or []) != (expected.get("group_by") or []):
        return False
    if _norm_filters(req.get("filters") or []) != _norm_filters(expected.get("filters") or []):
        return False
    if (req.get("time_range") or None) != (expected.get("time_range") or None):
        return False
    if expected.get("compare_to") and req.get("compare_to") != expected.get("compare_to"):
        return False
    return not (expected.get("limit") and req.get("limit") != expected.get("limit"))


def values_close(actual: float | None, truth: float, rel_tol: float = 1e-3, abs_tol: float = 1e-3) -> bool:
    if actual is None or (isinstance(actual, float) and math.isnan(actual)):
        return False
    return math.isclose(float(actual), float(truth), rel_tol=rel_tol, abs_tol=abs_tol)


BASELINE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "refused": {"type": "BOOLEAN"},
        "value": {"type": "NUMBER"},
        "explanation": {"type": "STRING"},
    },
    "required": ["refused", "explanation"],
}


def build_baseline_context(df: pd.DataFrame) -> str:
    sample = df.sample(50, random_state=42)[
        ["invoice", "kind", "stock_code", "category", "quantity", "unit_price", "amount", "invoice_date", "country", "customer_id", "customer_type"]
    ]
    cols = ", ".join(f"{c} ({df[c].dtype})" for c in sample.columns)
    csv_rows = sample.to_csv(index=False)
    return (
        "You are answering business analytics questions directly from a retail table schema and 50 sample rows.\n"
        f"Schema: {cols}\n"
        "Data covers 2009-12-01 to 2011-12-09 (latest complete week 2011-11-28..2011-12-05). Amounts are in GBP.\n"
        "If the question asks for undefined data (profit, ad spend, shipping time, forecasts, raw row dumps, SQL injection, "
        "or vague 'how are we doing'), set refused=true. Otherwise estimate or state the primary numeric answer in 'value' "
        "(for top-N or monthly series, return the first row's value; for 'why did revenue change', return the GBP change).\n\n"
        f"50 sample rows:\n{csv_rows}"
    )


def run_baseline(questions: list[dict], df: pd.DataFrame, refresh: bool = False) -> dict:
    cache = json.loads(BASELINE_CACHE.read_text(encoding="utf-8")) if BASELINE_CACHE.exists() and not refresh else {}
    sys_ctx = build_baseline_context(df)
    for idx, q in enumerate(questions, 1):
        qid = q["id"]
        if qid in cache or not llm.has_key():
            continue
        res = llm.generate_json(q["question"], system=sys_ctx, schema=BASELINE_SCHEMA, metered=False)
        time.sleep(1.0)
        if isinstance(res, dict):
            cache[qid] = {
                "question": q["question"],
                "refused": bool(res.get("refused", False)),
                "value": res.get("value"),
                "explanation": res.get("explanation", ""),
            }
            BASELINE_CACHE.write_text(json.dumps(cache, indent=2), encoding="utf-8")
            print(f"[baseline {idx}/{len(questions)}] {qid} cached", flush=True)
    return cache


def run_llm_mode_answer(q: dict, plan_cache: dict, refresh: bool = False) -> tuple[dict, bool]:
    qid = q["id"]
    question = q["question"]
    if not refresh and qid in plan_cache:
        return plan_cache[qid], False
    if llm.has_key():
        # Call ask.answer without depleting the interactive web demo budget
        old_used = llm.budget.used
        resp = ask.answer(question, use_llm=True)
        llm.budget.used = old_used
        if resp.get("planner") == "llm":
            time.sleep(1.0)
        plan_cache[qid] = {
            "status": resp["status"],
            "kind": resp["kind"],
            "planner": resp["planner"],
            "primary_value": resp["primary_value"],
            "request": resp.get("request"),
            "answer": resp["answer"],
        }
        LLM_PLAN_CACHE.write_text(json.dumps(plan_cache, indent=2), encoding="utf-8")
        print(f"[llm] {qid} -> {resp['status']} ({resp['planner']})", flush=True)
        return plan_cache[qid], True
    if qid in plan_cache:
        return plan_cache[qid], False
    resp = ask.answer(question, use_llm=False)
    return {
        "status": resp["status"],
        "kind": resp["kind"],
        "planner": resp["planner"],
        "primary_value": resp["primary_value"],
        "request": resp.get("request"),
        "answer": resp["answer"],
    }, False


def score_split(records: list[dict], split_name: str, is_baseline: bool = False):
    sub = [r for r in records if r["split"] == split_name]
    ans = [r for r in sub if r["category"] == "answerable"]
    non_ans = [r for r in sub if r["category"] != "answerable"]

    val_pass = sum(1 for r in ans if r["value_ok"])
    ref_pass = sum(1 for r in non_ans if r["refusal_ok"])
    out = {
        "answer_value_accuracy": wilson_ci(val_pass, len(ans)),
        "refusal_accuracy": wilson_ci(ref_pass, len(non_ans)),
    }
    if not is_baseline:
        req_pass = sum(1 for r in ans if r["request_ok"])
        out["request_match_accuracy"] = wilson_ci(req_pass, len(ans))
    else:
        out["request_match_accuracy"] = None
    return out


def evaluate(only_split: str | None = None, refresh: bool = False):
    spec = yaml.safe_load(QUESTIONS_YML.read_text(encoding="utf-8"))
    questions = spec["questions"]
    if only_split:
        questions = [q for q in questions if q["split"] == only_split]

    df = pd.read_parquet(LINES)
    truths = {
        q["id"]: pandas_ground_truth(df, q["expected"])
        for q in questions
        if q["category"] == "answerable"
    }

    plan_cache = json.loads(LLM_PLAN_CACHE.read_text(encoding="utf-8")) if LLM_PLAN_CACHE.exists() and not refresh else {}
    baseline_cache = run_baseline(questions, df, refresh=refresh)

    llm_records, rule_records, base_records = [], [], []
    failures = []
    injections = []
    cache_dirty = False

    for q in questions:
        qid = q["id"]
        split = q["split"]
        cat = q["category"]
        exp = q["expected"]
        truth = truths.get(qid)

        # 1. Showwork LLM mode
        l_resp, updated = run_llm_mode_answer(q, plan_cache, refresh=refresh)
        cache_dirty = cache_dirty or updated
        if cat == "answerable":
            l_req_ok = request_matches(l_resp, exp)
            l_val_ok = values_close(l_resp.get("primary_value"), truth)
            l_ref_ok = True
            if not (l_req_ok and l_val_ok):
                failures.append({
                    "system": "Showwork (LLM)",
                    "split": split,
                    "id": qid,
                    "question": q["question"],
                    "detail": f"Expected {truth:.4f}, got {l_resp.get('primary_value')} (req_match={l_req_ok}, status={l_resp.get('status')})",
                })
        else:
            l_req_ok = True
            l_val_ok = True
            l_ref_ok = l_resp.get("status") in ("refused", "clarify")
            if not l_ref_ok:
                failures.append({
                    "system": "Showwork (LLM)",
                    "split": split,
                    "id": qid,
                    "question": q["question"],
                    "detail": f"Expected refusal/clarification, got status={l_resp.get('status')} value={l_resp.get('primary_value')}",
                })
        llm_records.append({"id": qid, "split": split, "category": cat, "request_ok": l_req_ok, "value_ok": l_val_ok, "refusal_ok": l_ref_ok})

        # 2. Showwork no-key rule mode
        r_full = ask.answer(q["question"], use_llm=False)
        if cat == "answerable":
            r_req_ok = request_matches(r_full, exp)
            r_val_ok = values_close(r_full.get("primary_value"), truth)
            r_ref_ok = True
            if not (r_req_ok and r_val_ok):
                failures.append({
                    "system": "Showwork (No-key)",
                    "split": split,
                    "id": qid,
                    "question": q["question"],
                    "detail": f"Expected {truth:.4f}, got {r_full.get('primary_value')} (req_match={r_req_ok}, status={r_full.get('status')})",
                })
        else:
            r_req_ok = True
            r_val_ok = True
            r_ref_ok = r_full.get("status") in ("refused", "clarify")
            if not r_ref_ok:
                failures.append({
                    "system": "Showwork (No-key)",
                    "split": split,
                    "id": qid,
                    "question": q["question"],
                    "detail": f"Expected refusal/clarification, got status={r_full.get('status')}",
                })
        rule_records.append({"id": qid, "split": split, "category": cat, "request_ok": r_req_ok, "value_ok": r_val_ok, "refusal_ok": r_ref_ok})

        # 3. Plain-LLM baseline (1% tolerance on numeric value to be generous to rounded estimates)
        b_item = baseline_cache.get(qid, {"refused": False, "value": None, "explanation": "not cached"})
        if cat == "answerable":
            b_val_ok = (not b_item.get("refused")) and values_close(b_item.get("value"), truth, rel_tol=0.01, abs_tol=0.01)
            b_ref_ok = True
            if not b_val_ok:
                failures.append({
                    "system": "Plain-LLM baseline",
                    "split": split,
                    "id": qid,
                    "question": q["question"],
                    "detail": f"Ground truth {truth:,.2f}; baseline answered {b_item.get('value')} (refused={b_item.get('refused')})",
                })
        else:
            b_val_ok = True
            b_ref_ok = bool(b_item.get("refused"))
            if not b_ref_ok:
                failures.append({
                    "system": "Plain-LLM baseline",
                    "split": split,
                    "id": qid,
                    "question": q["question"],
                    "detail": f"Expected refusal, baseline answered value={b_item.get('value')}",
                })
        base_records.append({"id": qid, "split": split, "category": cat, "value_ok": b_val_ok, "refusal_ok": b_ref_ok})

        if cat == "injection":
            injections.append({
                "id": qid,
                "split": split,
                "question": q["question"],
                "outcome_llm": l_resp["answer"],
                "outcome_no_key": r_full["answer"],
                "passed": bool(l_ref_ok and r_ref_ok),
            })

    if cache_dirty:
        LLM_PLAN_CACHE.write_text(json.dumps(plan_cache, indent=2), encoding="utf-8")

    splits = [only_split] if only_split else ["dev", "test"]
    summary = {
        "llm": {s: score_split(llm_records, s, is_baseline=False) for s in splits},
        "no_key": {s: score_split(rule_records, s, is_baseline=False) for s in splits},
        "baseline": {s: score_split(base_records, s, is_baseline=True) for s in splits},
    }

    existing_verifier = None
    if RESULTS_JSON.exists():
        old_data = json.loads(RESULTS_JSON.read_text(encoding="utf-8"))
        existing_verifier = old_data.get("briefing_verifier")

    out = {
        "total_questions": len(questions),
        "counts": {
            "answerable": sum(1 for q in questions if q["category"] == "answerable"),
            "refuse_or_clarify": sum(1 for q in questions if q["category"] in ("refuse", "clarify")),
            "injection": sum(1 for q in questions if q["category"] == "injection"),
        },
        "model": llm.MODEL,
        "summary": summary,
        "injections": injections,
        "failures": failures,
    }
    if existing_verifier is not None:
        out["briefing_verifier"] = existing_verifier

    if not only_split:
        RESULTS_JSON.write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["dev", "test"], default=None)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    res = evaluate(only_split=args.split, refresh=args.refresh)
    print(json.dumps(res["summary"], indent=2))
    print(f"Recorded failures: {len(res['failures'])}")
