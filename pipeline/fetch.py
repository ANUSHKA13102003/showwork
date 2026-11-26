import json
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
XLSX = RAW / "online_retail_II.xlsx"
RAW_PARQUET = RAW / "online_retail_II.parquet"

# The Kaggle copy that is byte-for-byte the UCI workbook (two sheets, 2009-2010 and 2010-2011).
KAGGLE_REF = "lakshmi25npathi/online-retail-dataset"
UCI_URL = "https://archive.ics.uci.edu/static/public/502/online+retail+ii.zip"
SHEETS = ["Year 2009-2010", "Year 2010-2011"]


def kaggle_env():
    env = dict(os.environ)
    if env.get("KAGGLE_KEY") or env.get("KAGGLE_API_TOKEN"):
        return env
    cfg = Path.home() / ".kaggle" / "kaggle.json"
    if cfg.exists():
        # utf-8-sig: some editors save this file with a BOM, which the CLI rejects
        creds = json.loads(cfg.read_text(encoding="utf-8-sig"))
        env["KAGGLE_USERNAME"] = creds.get("username", "")
        env["KAGGLE_KEY"] = creds.get("key", "")
    return env


def from_kaggle():
    exe = shutil.which("kaggle") or str(Path(sys.executable).parent / "kaggle")
    cmd = [exe, "datasets", "download", "-d", KAGGLE_REF, "-p", str(RAW), "--unzip"]
    res = subprocess.run(cmd, env=kaggle_env(), capture_output=True, text=True, check=False)
    return res.returncode == 0 and XLSX.exists()


def from_uci():
    zpath = RAW / "uci.zip"
    urllib.request.urlretrieve(UCI_URL, zpath)
    with zipfile.ZipFile(zpath) as z:
        for name in z.namelist():
            if name.endswith(".xlsx"):
                XLSX.write_bytes(z.read(name))
    zpath.unlink()
    return XLSX.exists()


def fetch():
    RAW.mkdir(parents=True, exist_ok=True)
    if RAW_PARQUET.exists():
        return "cached"
    source = "kaggle"
    if not XLSX.exists():
        ok = False
        try:
            ok = from_kaggle()
        except OSError:
            ok = False
        if not ok:
            source = "uci"
            if not from_uci():
                raise SystemExit("Could not download Online Retail II from Kaggle or UCI.")
    frames = []
    for sheet in SHEETS:
        df = pd.read_excel(XLSX, sheet_name=sheet, dtype={"Invoice": str, "StockCode": str})
        df["source_sheet"] = sheet
        frames.append(df)
    raw = pd.concat(frames, ignore_index=True)
    raw["Description"] = raw["Description"].astype("string")
    raw["Country"] = raw["Country"].astype("string")
    raw.to_parquet(RAW_PARQUET, index=False)
    return source


if __name__ == "__main__":
    print(fetch())
