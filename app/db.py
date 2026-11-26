import json
import threading
from functools import lru_cache

import duckdb
import yaml

from app.config import LINES, META, METRICS_YML, WORK

_lock = threading.Lock()
_con = None


@lru_cache
def semantic():
    return yaml.safe_load(METRICS_YML.read_text(encoding="utf-8"))


@lru_cache
def meta():
    if META.exists():
        return json.loads(META.read_text(encoding="utf-8"))
    return {}


def _build(path, source):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    if tmp.exists():
        tmp.unlink()
    con = duckdb.connect(str(tmp))
    con.execute("CREATE TABLE lines AS SELECT * FROM read_parquet(?)", [str(source)])
    con.execute("CREATE TABLE returns AS SELECT * FROM lines WHERE kind = 'return'")
    con.execute("CREATE TABLE sales AS SELECT * FROM lines WHERE kind = 'sale'")
    con.close()
    tmp.replace(path)


def connect(source=LINES, db_path=None):
    """Read-only connection. The database file is rebuilt from the parquet when missing or stale."""
    global _con
    with _lock:
        if _con is not None and source == LINES and db_path is None:
            return _con
        path = db_path or WORK / "showwork.duckdb"
        if not path.exists() or path.stat().st_mtime < source.stat().st_mtime:
            _build(path, source)
        con = duckdb.connect(str(path), read_only=True, config={"enable_external_access": False})
        if source == LINES and db_path is None:
            _con = con
        return con


def query(sql, params=None, con=None):
    cur = (con or connect()).cursor()
    try:
        rel = cur.execute(sql, params or [])
        cols = [d[0] for d in rel.description]
        return cols, rel.fetchall()
    finally:
        cur.close()
