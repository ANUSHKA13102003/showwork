from datetime import date, timedelta


def money(v, decimals=None):
    if v is None:
        return "n/a"
    v = float(v)
    if decimals is None:
        decimals = 2 if abs(v) < 1000 else 0
    sign = "-" if v < 0 else ""
    return f"{sign}£{abs(v):,.{decimals}f}"


def count(v):
    return "n/a" if v is None else f"{round(float(v)):,}"


def pct(v, decimals=1):
    return "n/a" if v is None else f"{float(v) * 100:.{decimals}f}%"


def signed_pct(v, decimals=1):
    return "n/a" if v is None else f"{float(v) * 100:+.{decimals}f}%"


def signed_money(v):
    if v is None:
        return "n/a"
    v = float(v)
    return ("+" if v >= 0 else "-") + money(abs(v))


def value(v, unit):
    if unit == "gbp":
        return money(v)
    if unit == "ratio":
        return pct(v)
    return count(v)


def period(tr):
    if tr is None:
        return "across all data (Dec 2009 to Dec 2011)"
    start, end = date.fromisoformat(str(tr["start"])), date.fromisoformat(str(tr["end"]))
    last = end - timedelta(days=1)
    if start.day == 1 and end.day == 1:
        months = (end.year - start.year) * 12 + end.month - start.month
        if months == 1:
            return f"in {start:%B %Y}"
        if months == 12 and start.month == 1:
            return f"in {start.year}"
        if months == 3 and start.month in (1, 4, 7, 10):
            return f"in Q{(start.month - 1) // 3 + 1} {start.year}"
        return f"from {start:%b %Y} to {last:%b %Y}"
    if (end - start).days == 7 and start.weekday() == 0:
        return f"in the week of {start.day} {start:%b %Y}"
    return f"from {start.day} {start:%b %Y} to {last.day} {last:%b %Y}"
