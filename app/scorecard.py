import json

from app import db
from app.config import CATEGORY_REPORT, EVALS, QUALITY

RESULTS_JSON = EVALS / "results.json"


def load_scorecard():
    sem = db.semantic()
    meta = db.meta()
    quality = json.loads(QUALITY.read_text(encoding="utf-8")) if QUALITY.exists() else {}
    cats = json.loads(CATEGORY_REPORT.read_text(encoding="utf-8")) if CATEGORY_REPORT.exists() else {}
    evals_data = json.loads(RESULTS_JSON.read_text(encoding="utf-8")) if RESULTS_JSON.exists() else None

    metrics_list = [
        {
            "key": k,
            "label": v["label"],
            "unit": v["unit"],
            "definition": v["definition"],
            "sql": v["sql"],
            "owner": v["owner"],
            "grain": v["grain"],
            "caveats": v["caveats"],
            "definition_version": v["definition_version"],
        }
        for k, v in sem["metrics"].items()
    ]
    dimensions_list = [
        {"key": k, "label": v["label"], "sql": v["sql"], "description": v["description"]}
        for k, v in sem["dimensions"].items()
    ]

    quarantine_rows = []
    reasons = quality.get("quarantine_by_reason", {})
    descs = quality.get("reason_descriptions", {})
    for code, count in reasons.items():
        quarantine_rows.append({
            "code": code,
            "count": count,
            "share_of_input": round(count / quality["input_rows"], 5) if quality.get("input_rows") else 0.0,
            "description": descs.get(code, ""),
        })

    return {
        "as_of": meta.get("as_of"),
        "source": meta.get("source"),
        "evals": evals_data,
        "metrics": metrics_list,
        "dimensions": dimensions_list,
        "quality": {
            "input_rows": quality.get("input_rows", 0),
            "clean_rows": quality.get("clean_rows", 0),
            "sale_rows": quality.get("sale_rows", 0),
            "return_rows": quality.get("return_rows", 0),
            "quarantined_rows": quality.get("quarantined_rows", 0),
            "flags": quality.get("flags", {}),
            "descriptions": quality.get("descriptions", {}),
            "reasons": quarantine_rows,
        },
        "categories": {
            "products": cats.get("products", 0),
            "review_queue": cats.get("review_queue", 0),
            "stages": cats.get("stages", []),
            "llm_consistency_check": cats.get("llm_consistency_check"),
        },
    }
