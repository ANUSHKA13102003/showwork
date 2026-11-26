import math
import re

DATE_PATTERNS = [
    re.compile(r"\b20\d\d-\d{2}-\d{2}\b"),
    re.compile(
        r"\b\d{1,2}\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
        r"(?:\s+20\d\d)?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
        r"\s+\d{1,2}(?:,\s*20\d\d)?\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b[48]-week\b", re.IGNORECASE),
    re.compile(r"\bprior\s+[48]\s+weeks\b", re.IGNORECASE),
    re.compile(r"\blast\s+[48]\s+weeks\b", re.IGNORECASE),
    re.compile(r"\b20(?:09|10|11)\b"),
    re.compile(r"\bv\d+\b", re.IGNORECASE),
]

NUM_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(?P<sign>[+\-])?\s*"
    r"(?P<curr>£|GBP\s*)?"
    r"(?P<num>\d{1,3}(?:,\d{3})+|\d+)(?P<dec>\.\d+)?"
    r"\s*(?P<suf>%|percent|k|m|bn|gbp)?"
    r"(?![A-Za-z0-9_])",
    re.IGNORECASE,
)


def strip_dates_and_labels(text: str) -> str:
    cleaned = text
    for rx in DATE_PATTERNS:
        cleaned = rx.sub(" ", cleaned)
    return cleaned


def extract_numbers(text: str) -> list[dict]:
    cleaned = strip_dates_and_labels(text)
    items = []
    for m in NUM_TOKEN.finditer(cleaned):
        raw_str = m.group(0).strip()
        num_s = (m.group("num") or "").replace(",", "") + (m.group("dec") or "")
        val = float(num_s)
        suf = (m.group("suf") or "").lower()
        curr = bool(m.group("curr")) or suf == "gbp"
        kind = "number"
        if suf in ("%", "percent"):
            kind = "percent"  # stored in percentage points, e.g. 14.2 for 14.2%
        elif suf == "k":
            val *= 1_000.0
            kind = "gbp" if curr else "number"
        elif suf == "m":
            val *= 1_000_000.0
            kind = "gbp" if curr else "number"
        elif suf == "bn":
            val *= 1_000_000_000.0
            kind = "gbp" if curr else "number"
        elif curr:
            kind = "gbp"
        items.append({
            "raw": raw_str,
            "abs_value": abs(val),
            "signed_value": -val if m.group("sign") == "-" else val,
            "kind": kind,
            "has_shorthand": suf in ("k", "m", "bn"),
        })
    return items


def collect_fact_values(facts: dict) -> dict[str, list[float]]:
    """Collect allowed magnitudes from computed briefing facts by unit family."""
    gbp_vals: list[float] = []
    pct_vals: list[float] = []  # in percentage points, e.g. 14.23
    num_vals: list[float] = []

    def add(val, unit: str):
        if val is None or (isinstance(val, float) and math.isnan(val)):
            return
        fv = abs(float(val))
        if unit == "gbp":
            gbp_vals.append(fv)
            num_vals.append(fv)
        elif unit == "ratio":
            pct_vals.append(fv * 100.0)
        else:
            num_vals.append(fv)

    for kpi in facts.get("kpis", []):
        u = kpi["unit"]
        add(kpi.get("current"), u)
        add(kpi.get("wow_prev"), u)
        add(kpi.get("wow_change"), u)
        add(kpi.get("wow_pct"), "ratio")
        add(kpi.get("avg4"), u)
        add(kpi.get("vs_avg4_change"), u)
        add(kpi.get("vs_avg4_pct"), "ratio")

    drv = facts.get("drivers", {})
    add(drv.get("current"), "gbp")
    add(drv.get("previous"), "gbp")
    add(drv.get("change"), "gbp")
    add(drv.get("change_pct"), "ratio")
    for dim_info in drv.get("dimensions", {}).values():
        for mem in dim_info.get("members", []):
            add(mem.get("current"), "gbp")
            add(mem.get("previous"), "gbp")
            add(mem.get("change"), "gbp")

    for anom in facts.get("anomalies", []):
        u = anom.get("unit", "number")
        add(anom.get("current"), u)
        add(anom.get("median_8w"), u)
        add(anom.get("mad_8w"), u)
        if anom.get("mad_score") is not None:
            num_vals.append(abs(float(anom["mad_score"])))

    for w in facts.get("watchlist", []):
        for fv_item in w.get("fact_numbers", []):
            add(fv_item.get("value"), fv_item.get("unit", "number"))

    return {"gbp": gbp_vals, "percent": pct_vals, "number": num_vals}


def _matches_any(token: dict, pool: dict[str, list[float]]) -> bool:
    v = token["abs_value"]
    kind = token["kind"]
    if kind == "percent":
        # Match percentage points within 0.15 pp (e.g. 14.23% written as 14.2% or 14.23%)
        return any(math.isclose(v, target, rel_tol=0.015, abs_tol=0.15) for target in pool["percent"])

    candidates = pool["gbp"] if kind == "gbp" else (pool["number"] + pool["gbp"])
    rel = 0.02 if token["has_shorthand"] else 0.005
    abs_tol = 1.0 if kind == "gbp" else 0.55
    return any(math.isclose(v, target, rel_tol=rel, abs_tol=abs_tol) for target in candidates)


def verify(text: str, facts: dict, max_words: int = 200) -> dict:
    words = text.split()
    if not words:
        return {"ok": False, "word_count": 0, "extracted": [], "unmatched": ["empty_summary"]}
    if len(words) > max_words:
        return {
            "ok": False,
            "word_count": len(words),
            "extracted": [],
            "unmatched": [f"exceeded_word_limit:{len(words)}"],
        }
    extracted = extract_numbers(text)
    if not extracted:
        return {"ok": False, "word_count": len(words), "extracted": [], "unmatched": ["no_numbers_cited"]}
    pool = collect_fact_values(facts)
    unmatched = [t["raw"] for t in extracted if not _matches_any(t, pool)]
    return {
        "ok": len(unmatched) == 0,
        "word_count": len(words),
        "extracted": [t["raw"] for t in extracted],
        "unmatched": unmatched,
    }
