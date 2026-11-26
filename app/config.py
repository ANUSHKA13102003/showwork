import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LINES = DATA / "lines.parquet"
META = DATA / "meta.json"
WORK = DATA / "work"
PIPELINE = ROOT / "pipeline"
METRICS_YML = PIPELINE / "metrics.yml"
QUALITY = PIPELINE / "results" / "quality.json"
CATEGORY_REPORT = PIPELINE / "results" / "categories.json"
BRIEFINGS = ROOT / "briefings"
EVALS = ROOT / "evals"
STATIC = Path(__file__).resolve().parent / "static"


def load_env(path=ROOT / ".env"):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()


def flag(name, default="0"):
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def setting(name, default):
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return type(default)(raw)
