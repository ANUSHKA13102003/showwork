import re
from datetime import date, timedelta

from app import db

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"], start=1)}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})
MONTHS["sept"] = 9
MONTH_RX = "|".join(sorted(MONTHS, key=len, reverse=True))

INJECTION = re.compile(
    r"ignore\s+(all\s+|any\s+|the\s+|your\s+)?(previous\s+|prior\s+|above\s+|earlier\s+)?(instructions|rules|prompt)"
    r"|disregard|system\s+prompt|you\s+are\s+now|jailbreak|developer\s+mode|pretend\s+to\s+be"
    r"|\bdrop\s+table|\bdelete\s+from|\binsert\s+into|\bupdate\s+\w+\s+set|\balter\s+table|\btruncate\b"
    r"|;\s*--|union\s+select|select\s+\*|\braw\s+(rows|data|records|table)|\ball\s+(the\s+)?rows|\bevery\s+row"
    r"|full\s+table|\bdump\b|export\s+(the\s+)?(table|data|database)|customer\s+(ids|list|names|emails)"
    r"|list\s+(all\s+)?(the\s+)?customers|personal\s+data|api\s+key|password|credential|<script",
    re.IGNORECASE,
)
UNDEFINED = re.compile(
    r"\b(profit|margin|cost|costs|cogs|inventory|stock\s+levels?|marketing|ad\s+spend|conversion|traffic|visits"
    r"|sessions|clicks|churn|ltv|lifetime\s+value|forecast|predict|projection|next\s+(week|month|quarter|year)"
    r"|salary|salaries|employees?|weather|nps|satisfaction|ratings?|discounts?|shipping\s+time|delivery\s+time)\b",
    re.IGNORECASE,
)
PRODUCT_LEVEL = re.compile(r"\b(which|top|best[- ]selling|best)\s+(\d+\s+)?(products?|items?|skus?)\b|\bbest\s*sellers?\b|\bsku\b", re.IGNORECASE)
VAGUE = re.compile(r"how\s+(are|is)\s+(we|it|the\s+business|business)\s+doing|performance|doing\s+well|kpis?\b"
                   r"|show\s+me\s+(the\s+)?numbers|give\s+me\s+(a\s+)?summary|how\s+did\s+we\s+do|what'?s\s+up", re.IGNORECASE)

METRIC_PATTERNS = [
    ("return_rate", r"return\s+rate|refund\s+rate|share\s+(of\s+revenue\s+)?returned|returns?\s+as\s+a\s+(share|percent)"),
    ("repeat_customer_rate", r"repeat\s+(customer\s+)?(rate|purchase)|returning\s+customer\s+(rate|share)|share\s+of\s+returning"),
    ("aov", r"average\s+order\s+value|\baov\b|average\s+basket|avg\.?\s+order|average\s+order\s+size|basket\s+size"),
    ("gross_revenue", r"gross\s+(revenue|sales)"),
    ("net_revenue", r"net\s+(revenue|sales)"),
    ("returns", r"\breturns\b|\brefunds?\b|\breturned\b|cancell?ations?|\bcancell?ed\b"),
    ("orders", r"\borders?\b|\binvoices?\b|\btransactions?\b"),
    ("units", r"\bunits?\b|items\s+sold|\bquantity\b|\bpieces\b|products\s+sold"),
    ("customers", r"\bcustomers?\b|\bbuyers?\b|\bshoppers?\b|\bclients?\b"),
    ("net_revenue", r"\brevenue\b|\bsales\b|\bturnover\b|\btakings\b|\bincome\b|\bearn|\bmake\b|\bmade\b|\bsold\b"),
]
GROUP_PATTERNS = [
    ("country", r"by\s+country|per\s+country|each\s+country|which\s+countr|top\s+(\d+\s+)?countr|countries|by\s+market|which\s+market"),
    ("category", r"by\s+category|per\s+category|each\s+category|which\s+categor|top\s+(\d+\s+)?categor|categories"),
    ("customer_type", r"by\s+customer\s+type|customer\s+types?|new\s+vs\.?\s+returning|new\s+versus\s+returning|split\s+by\s+customer"),
    ("week", r"by\s+week|weekly|each\s+week|per\s+week|week\s+by\s+week|week-by-week"),
    ("month", r"by\s+month|monthly|each\s+month|per\s+month|month\s+by\s+month|month-by-month|\btrend\b|over\s+time"),
]
COUNTRY_ALIASES = {"uk": "United Kingdom", "britain": "United Kingdom", "england": "United Kingdom",
                   "ireland": "EIRE", "eire": "EIRE", "us": "USA", "united states": "USA", "america": "USA",
                   "south africa": "RSA", "holland": "Netherlands", "uae": "United Arab Emirates"}


def ctx():
    m = db.meta()
    return {
        "min": date.fromisoformat(m["min_date"]),
        "max": date.fromisoformat(m["max_date"]),
        "week": date.fromisoformat(m["latest_complete_week"]),
        "countries": m["countries"],
        "categories": m["categories"],
    }


def month_range(y, m):
    end = date(y + (m == 12), m % 12 + 1, 1)
    return {"start": date(y, m, 1).isoformat(), "end": end.isoformat()}


def latest_complete_month(c):
    nxt = date(c["max"].year + (c["max"].month == 12), c["max"].month % 12 + 1, 1)
    if c["max"] + timedelta(days=1) == nxt:
        return c["max"].year, c["max"].month
    first = date(c["max"].year, c["max"].month, 1) - timedelta(days=1)
    return first.year, first.month


def parse_time(q, c):
    s = q.lower()
    wk = c["week"]
    m = re.search(r"week\s+(of|starting|beginning)\s+(\d{4}-\d{2}-\d{2})", s)
    if m:
        d = date.fromisoformat(m.group(2))
        start = d - timedelta(days=d.weekday())
        return {"start": start.isoformat(), "end": (start + timedelta(days=7)).isoformat()}
    m = re.search(r"(last|past|previous)\s+(\d+)\s+weeks", s)
    if m:
        n = int(m.group(2))
        return {"start": (wk - timedelta(days=7 * (n - 1))).isoformat(), "end": (wk + timedelta(days=7)).isoformat()}
    if re.search(r"\b(last|this|latest|past)\s+week\b", s):
        return {"start": wk.isoformat(), "end": (wk + timedelta(days=7)).isoformat()}
    m = re.search(r"(last|past|previous)\s+(\d+)\s+months", s)
    if m:
        y, mo = latest_complete_month(c)
        n = int(m.group(2))
        sy, sm = divmod(y * 12 + mo - 1 - (n - 1), 12)
        return {"start": date(sy, sm + 1, 1).isoformat(), "end": month_range(y, mo)["end"]}
    if re.search(r"\b(last|latest|past)\s+month\b", s):
        return month_range(*latest_complete_month(c))
    if re.search(r"\bthis\s+month\b", s):
        return month_range(c["max"].year, c["max"].month)
    m = re.search(r"\bq([1-4])\s*(?:of\s+)?(20\d\d)\b|\b(20\d\d)\s*q([1-4])\b", s)
    if m:
        qn, y = (int(m.group(1)), int(m.group(2))) if m.group(1) else (int(m.group(4)), int(m.group(3)))
        sm = 3 * (qn - 1) + 1
        return {"start": date(y, sm, 1).isoformat(), "end": month_range(y, sm + 2)["end"]}
    m = re.search(rf"\b({MONTH_RX})\b\.?,?\s*(?:of\s+)?(20\d\d)\b", s)
    if m:
        return month_range(int(m.group(2)), MONTHS[m.group(1)])
    m = re.search(rf"\b({MONTH_RX})\b", s)
    if m and not re.search(r"\bmay\s+(i|we|you)\b", s):
        mo = MONTHS[m.group(1)]
        y = c["max"].year if date(c["max"].year, mo, 1) <= c["max"] else c["max"].year - 1
        return month_range(y, mo)
    m = re.search(r"\b(?:in|for|during|of)?\s*(20\d\d)\b", s)
    if m:
        y = int(m.group(1))
        return {"start": date(y, 1, 1).isoformat(), "end": date(y + 1, 1, 1).isoformat()}
    if re.search(r"\blast\s+year\b", s):
        y = c["max"].year - 1
        return {"start": date(y, 1, 1).isoformat(), "end": date(y + 1, 1, 1).isoformat()}
    if re.search(r"\bthis\s+year\b", s):
        y = c["max"].year
        return {"start": date(y, 1, 1).isoformat(), "end": date(y + 1, 1, 1).isoformat()}
    return None


def parse_compare(s):
    if (
        re.search(r"last\s+year|previous\s+year|prior\s+year|year\s+before|\byoy\b|year[- ]over[- ]year|same\s+(week|month|period)\s+(last|a)\s+year", s)
        and re.search(r"(vs\.?|versus|compared|against|than|change|grow|growth|from)", s)
    ):
        return "previous_year"
    if re.search(r"(vs\.?|versus|compared\s+(to|with)|against)\s+(the\s+)?(previous|prior|last|week|month|before)"
                 r"|week[- ]over[- ]week|month[- ]over[- ]month|\bwow\b|\bmom\b|\bchange[ds]?\b|\bgr[eo]w|growth"
                 r"|increase|decrease|\bdrop|\bfell\b|\bfall\b|\brise\b|\brose\b|\bup\s+or\s+down", s):
        return "previous_period"
    return None


def find_filters(q, c, metric):
    s = q.lower()
    out = []
    countries = []
    for name in c["countries"]:
        if re.search(rf"\b{re.escape(name.lower())}\b", s):
            countries.append(name)
    for alias, name in COUNTRY_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", s) and name in c["countries"] and name not in countries:
            if alias == "us" and not re.search(r"\b(the\s+us|in\s+us|us\s+customers)\b", s):
                continue
            countries.append(name)
    if countries:
        out.append({"dimension": "country", "values": countries})
    cats = [name for name in c["categories"] if re.search(rf"\b{re.escape(name.lower())}\b", s)]
    if not cats:
        first_words = {name.split()[0].lower(): name for name in c["categories"] if name != "Other"}
        cats = [name for word, name in first_words.items() if re.search(rf"\b{re.escape(word)}\b", s)]
    if cats:
        out.append({"dimension": "category", "values": cats})
    types = []
    if re.search(r"\bnew\s+(customers?|buyers?|shoppers?)\b|\bfrom\s+new\b|\bfirst[- ]time\b", s):
        types.append("new")
    if re.search(r"\breturning\s+(customers?|buyers?)\b|\bexisting\s+customers?\b", s) and metric != "repeat_customer_rate":
        types.append("returning")
    if re.search(r"\bguests?\b", s):
        types.append("guest")
    if types and not re.search(r"new\s+vs\.?\s+returning|new\s+versus\s+returning", s):
        out.append({"dimension": "customer_type", "values": types})
    return out


def find_metric(s):
    for metric, rx in METRIC_PATTERNS:
        if re.search(rx, s):
            return metric
    return None


def plan(question):
    """Rule-based question to plan. Returns a dict with action query, why, clarify or refuse."""
    s = question.lower().strip()
    c = ctx()
    if INJECTION.search(s):
        return {"action": "refuse", "kind": "injection"}
    if UNDEFINED.search(s):
        return {"action": "refuse", "kind": "undefined", "term": UNDEFINED.search(s).group(0)}
    if PRODUCT_LEVEL.search(s):
        return {"action": "refuse", "kind": "product_level"}
    metric = find_metric(s)
    time_range = parse_time(question, c)
    if re.search(r"\bwhy\b|what\s+drove|drivers?\s+of|explain\s+the\s+(change|drop|increase)|what\s+caused", s):
        if metric in (None, "net_revenue", "gross_revenue", "returns", "units"):
            tr = time_range or {"start": c["week"].isoformat(), "end": (c["week"] + timedelta(days=7)).isoformat()}
            return {"action": "why", "request": {
                "metric": metric or "net_revenue", "group_by": [], "filters": find_filters(question, c, metric),
                "time_range": tr, "compare_to": parse_compare(s) or "previous_period"}}
        return {"action": "clarify", "kind": "why_metric"}
    if metric is None:
        if VAGUE.search(s) or re.search(r"\b(best|top|worst|biggest)\b", s):
            return {"action": "clarify", "kind": "vague"}
        return {"action": "refuse", "kind": "no_metric"}
    group_by = [d for d, rx in GROUP_PATTERNS if re.search(rx, s)][:2]
    filters = find_filters(question, c, metric)
    limit = 20
    m = re.search(r"\b(top|bottom)\s+(\d+)\b", s)
    if m:
        limit = int(m.group(2))
    elif group_by and re.search(r"\bwhich\b|\bbiggest\b|\blargest\b|\bhighest\b|\bmost\b|\btop\b|\bbest\b", s) \
            and not any(g in ("week", "month") for g in group_by):
        limit = 1 if re.search(r"\bwhich\s+\w+\s+(had|has|was|is|brought|generated|made)\b|\bthe\s+(most|highest|biggest|largest|top)\b", s) else 20
    sort = "asc" if re.search(r"\b(lowest|least|bottom|smallest|worst|fewest)\b", s) else "desc"
    if sort == "asc" and limit == 20 and group_by and not any(g in ("week", "month") for g in group_by):
        limit = 1 if re.search(r"\bwhich\b|\bthe\s+(lowest|least|smallest|fewest)\b", s) else 20
    compare_to = parse_compare(s) if time_range else None
    req = {"metric": metric, "group_by": group_by, "filters": filters, "time_range": time_range,
           "compare_to": compare_to, "sort": sort, "limit": limit}
    return {"action": "query", "request": req}
