import csv
import io
import re
import statistics

import pandas as pd
from fastapi import HTTPException, UploadFile
from rapidfuzz import fuzz

from app.config import LINES

MAX_CSV_BYTES = 5 * 1024 * 1024  # 5 MB
FORMULA_PREFIXES = ("=", "+", "-", "@")
SAFE_NEGATIVE_NUM = re.compile(r"^-\d+(?:\.\d+)?$")
PRICE_COL_RX = re.compile(r"price|amount|total|cost|revenue", re.IGNORECASE)
QTY_COL_RX = re.compile(r"qty|quantity|units", re.IGNORECASE)
NULL_TOKENS = {"", "na", "n/a", "null", "none", "nan", "-"}


async def read_validated_csv(upload: UploadFile) -> tuple[str, pd.DataFrame]:
    name = (upload.filename or "").strip()
    if not name.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only .csv files are accepted.")
    raw = await upload.read(MAX_CSV_BYTES + 1)
    if len(raw) > MAX_CSV_BYTES:
        raise HTTPException(status_code=413, detail="CSV exceeds the 5 MB upload limit.")
    if not raw.strip():
        raise HTTPException(status_code=400, detail="Uploaded CSV is empty.")

    text = None
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise HTTPException(status_code=400, detail="Could not decode CSV text.")

    try:
        df = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid CSV structure: {exc}") from exc

    if df.empty or len(df.columns) == 0:
        raise HTTPException(status_code=400, detail="CSV contains no data rows.")
    return name, df


def _is_missing_series(s: pd.Series) -> pd.Series:
    return s.astype(str).str.strip().str.lower().isin(NULL_TOKENS)


def _numeric_parse(s: pd.Series) -> tuple[pd.Series, pd.Series]:
    missing = _is_missing_series(s)
    cleaned = s.astype(str).str.strip().str.replace(",", "", regex=False).str.lstrip("£$")
    parsed = pd.to_numeric(cleaned, errors="coerce")
    non_missing = ~missing
    valid_num = non_missing & parsed.notna()
    invalid_num = non_missing & parsed.isna()
    return parsed, invalid_num & (valid_num.sum() >= max(3, int(0.70 * non_missing.sum())))


def _casing_and_typo_map(s: pd.Series) -> dict[str, str]:
    missing = _is_missing_series(s)
    vals = s[~missing].astype(str)
    if vals.empty:
        return {}
    # Step 1: group by normalised whitespace + case, pick most frequent surface form (or Title/Upper canonical)
    norm_key = vals.str.strip().str.replace(r"\s+", " ", regex=True).str.lower()
    mapping: dict[str, str] = {}
    canon_counts: dict[str, int] = {}

    for key, grp in vals.groupby(norm_key):
        if not key:
            continue
        # Prefer trimmed version of the most common spelling in the group
        trimmed = grp.str.strip().str.replace(r"\s+", " ", regex=True)
        vc = trimmed.value_counts()
        # If mixed case exists, pick the most frequent non-all-lowercase spelling if available
        non_lower = [k for k in vc.index if k != k.lower()]
        best = non_lower[0] if non_lower and vc[non_lower[0]] >= vc.iloc[0] else vc.index[0]
        if len(set(trimmed)) > 1 and best == best.lower() and best.replace(" ", "").isalpha():
            best = best.title()
        canon_counts[best] = len(grp)
        for raw_v in grp.unique():
            if raw_v != best:
                mapping[raw_v] = best

    # Step 2: rapidfuzz typo merge for low-cardinality categorical text columns (<= 80 unique canonical values)
    distinct = sorted(canon_counts, key=lambda k: (-canon_counts[k], k))
    if 2 <= len(distinct) <= 80:
        merged_to: dict[str, str] = {}
        for i, rare in enumerate(distinct):
            if len(rare) < 5 or canon_counts[rare] > 2:
                continue
            for common in distinct[:i]:
                if common in merged_to or canon_counts[common] < 3:
                    continue
                if abs(len(rare) - len(common)) <= 2 and fuzz.ratio(rare.lower(), common.lower()) >= 90:
                    merged_to[rare] = common
                    break
        if merged_to:
            for raw_v in vals.unique():
                cur = mapping.get(raw_v, raw_v.strip())
                if cur in merged_to:
                    mapping[raw_v] = merged_to[cur]
                elif raw_v in merged_to:
                    mapping[raw_v] = merged_to[raw_v]
    return mapping


def detect_issues(df: pd.DataFrame) -> list[dict]:
    issues: list[dict] = []

    # 1. Exact duplicate rows
    dup_mask = df.duplicated(keep="first")
    dup_cnt = int(dup_mask.sum())
    if dup_cnt > 0:
        issues.append({
            "id": "duplicates",
            "kind": "duplicates",
            "column": "All columns",
            "title": f"{dup_cnt} exact duplicate row{'s' if dup_cnt != 1 else ''}",
            "count": dup_cnt,
            "rows_removed_estimate": dup_cnt,
            "proposed_fix": "Keep first occurrence and remove exact duplicate rows.",
            "default_accepted": True,
            "examples": df[dup_mask].head(2).to_dict(orient="records"),
        })

    for col in df.columns:
        s = df[col]
        missing = _is_missing_series(s)
        miss_cnt = int(missing.sum())
        parsed, mixed_mask = _numeric_parse(s)
        non_missing_cnt = int((~missing).sum())
        is_mostly_numeric = non_missing_cnt > 0 and int(parsed.notna().sum()) >= max(3, int(0.70 * non_missing_cnt))

        # 2. Mixed types in numeric columns
        mixed_cnt = int(mixed_mask.sum())
        if mixed_cnt > 0:
            examples = s[mixed_mask].unique()[:4].tolist()
            issues.append({
                "id": f"mixed_types:{col}",
                "kind": "mixed_types",
                "column": col,
                "title": f"{mixed_cnt} non-numeric value{'s' if mixed_cnt != 1 else ''} in numeric column '{col}'",
                "count": mixed_cnt,
                "rows_removed_estimate": 0,
                "proposed_fix": f"Extract leading numeric value where unambiguous or set invalid entries in '{col}' to median.",
                "default_accepted": True,
                "examples": examples,
            })

        # 3. Missing values
        if miss_cnt > 0:
            fix_desc = (
                f"Impute missing '{col}' values with column median."
                if is_mostly_numeric
                else f"Fill missing '{col}' cells with 'Unknown'."
            )
            issues.append({
                "id": f"missing_values:{col}",
                "kind": "missing_values",
                "column": col,
                "title": f"{miss_cnt} missing value{'s' if miss_cnt != 1 else ''} in '{col}'",
                "count": miss_cnt,
                "rows_removed_estimate": 0,
                "proposed_fix": fix_desc,
                "default_accepted": True,
                "examples": [],
            })

        # 4. Inconsistent casing, whitespace, and near-duplicate spelling
        if not is_mostly_numeric:
            cmap = _casing_and_typo_map(s)
            if cmap:
                affected = int(s.isin(cmap).sum())
                sample_pairs = [f"'{k}' -> '{v}'" for k, v in list(cmap.items())[:3]]
                issues.append({
                    "id": f"casing_spelling:{col}",
                    "kind": "inconsistent_casing_and_spelling",
                    "column": col,
                    "title": f"{affected} cell{'s' if affected != 1 else ''} with inconsistent casing, whitespace, or typos in '{col}'",
                    "count": affected,
                    "rows_removed_estimate": 0,
                    "proposed_fix": f"Trim whitespace, standardise casing, and merge near-duplicate spellings in '{col}'.",
                    "default_accepted": True,
                    "examples": sample_pairs,
                })

        # 5. Impossible values (non-positive prices or zero quantities / negative quantities on non-cancelled invoices)
        if is_mostly_numeric:
            inv_col = next((c for c in df.columns if "invoice" in c.lower()), None)
            is_cancel = df[inv_col].astype(str).str.strip().str.upper().str.startswith("C") if inv_col else pd.Series(False, index=df.index)
            imp_mask = pd.Series(False, index=df.index)
            reason_txt = ""
            if PRICE_COL_RX.search(col):
                imp_mask = parsed.notna() & (parsed <= 0)
                reason_txt = f"Remove rows where '{col}' <= 0."
            elif QTY_COL_RX.search(col):
                imp_mask = parsed.notna() & ((parsed == 0) | ((parsed < 0) & (~is_cancel)))
                reason_txt = f"Remove non-cancellation rows where '{col}' <= 0."
            imp_cnt = int(imp_mask.sum())
            if imp_cnt > 0:
                issues.append({
                    "id": f"impossible_values:{col}",
                    "kind": "impossible_values",
                    "column": col,
                    "title": f"{imp_cnt} impossible value{'s' if imp_cnt != 1 else ''} in '{col}'",
                    "count": imp_cnt,
                    "rows_removed_estimate": imp_cnt,
                    "proposed_fix": reason_txt,
                    "default_accepted": True,
                    "examples": s[imp_mask].head(3).tolist(),
                })

            # 6. Statistical outliers (median + 6 * scaled MAD on positive values, min 12 valid rows)
            pos_vals = parsed[parsed.notna() & (parsed > 0)].tolist()
            if len(pos_vals) >= 12 and (PRICE_COL_RX.search(col) or QTY_COL_RX.search(col)):
                med = statistics.median(pos_vals)
                mad = statistics.median([abs(x - med) for x in pos_vals])
                sigma = 1.4826 * mad if mad > 0 else max(abs(med) * 0.5, 1.0)
                upper = med + 8.0 * sigma
                out_mask = parsed.notna() & (parsed > upper) & (parsed > med * 5)
                out_cnt = int(out_mask.sum())
                if out_cnt > 0:
                    issues.append({
                        "id": f"outliers:{col}",
                        "kind": "outliers",
                        "column": col,
                        "title": f"{out_cnt} extreme outlier{'s' if out_cnt != 1 else ''} in '{col}' (> {upper:,.2f})",
                        "count": out_cnt,
                        "rows_removed_estimate": 0,
                        "proposed_fix": f"Cap extreme '{col}' outliers at upper robust threshold ({upper:,.2f}).",
                        "default_accepted": False,
                        "examples": s[out_mask].head(3).tolist(),
                    })

    return issues


def neutralise_cell(val: object) -> str:
    txt = "" if val is None else str(val)
    stripped = txt.lstrip(" \t\r\n")
    if not stripped:
        return txt
    if stripped[0] in ("=", "+", "@"):
        return "'" + txt
    if stripped[0] == "-" and not SAFE_NEGATIVE_NUM.match(stripped):
        return "'" + txt
    return txt


def to_safe_csv(df: pd.DataFrame) -> tuple[str, int]:
    neutralised = 0
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow([neutralise_cell(c) for c in df.columns])
    for row in df.itertuples(index=False, name=None):
        out_row = []
        for cell in row:
            safe = neutralise_cell(cell)
            if safe != ("" if cell is None else str(cell)):
                neutralised += 1
            out_row.append(safe)
        writer.writerow(out_row)
    return buf.getvalue(), neutralised


def apply_fixes(df: pd.DataFrame, accepted_ids: set[str], preview_only: bool = False) -> dict:
    work = df.copy()
    input_rows = len(work)
    cells_modified = 0
    log_lines = [f"Input dataset: {input_rows} rows, {len(work.columns)} columns."]

    # 1. Duplicates
    if "duplicates" in accepted_ids:
        before = len(work)
        work = work.drop_duplicates(keep="first").reset_index(drop=True)
        removed = before - len(work)
        if removed:
            log_lines.append(f"[duplicates] Removed {removed} exact duplicate rows ({len(work)} rows remaining).")

    for col in list(work.columns):
        # 2. Mixed types
        if f"mixed_types:{col}" in accepted_ids:
            parsed, mixed_mask = _numeric_parse(work[col])
            cnt = int(mixed_mask.sum())
            if cnt:
                valid_vals = parsed[parsed.notna()].tolist()
                med = statistics.median(valid_vals) if valid_vals else 0.0
                extracted_nums = work.loc[mixed_mask, col].astype(str).str.extract(r"([+\-]?\d+(?:\.\d+)?)")[0]
                replacement = extracted_nums.fillna(f"{med:g}")
                work.loc[mixed_mask, col] = replacement
                cells_modified += cnt
                log_lines.append(f"[mixed_types:{col}] Coerced {cnt} non-numeric entries in '{col}'.")

        # 3. Impossible values (row removal)
        if f"impossible_values:{col}" in accepted_ids:
            parsed, _ = _numeric_parse(work[col])
            inv_col = next((c for c in work.columns if "invoice" in c.lower()), None)
            is_cancel = (
                work[inv_col].astype(str).str.strip().str.upper().str.startswith("C")
                if inv_col
                else pd.Series(False, index=work.index)
            )
            if PRICE_COL_RX.search(col):
                bad = parsed.notna() & (parsed <= 0)
            else:
                bad = parsed.notna() & ((parsed == 0) | ((parsed < 0) & (~is_cancel)))
            removed = int(bad.sum())
            if removed:
                work = work[~bad].reset_index(drop=True)
                log_lines.append(f"[impossible_values:{col}] Removed {removed} rows with impossible '{col}' values ({len(work)} rows remaining).")

        # 4. Missing values
        if f"missing_values:{col}" in accepted_ids:
            missing = _is_missing_series(work[col])
            cnt = int(missing.sum())
            if cnt:
                parsed, _ = _numeric_parse(work[col])
                non_miss = int((~missing).sum())
                is_num = non_miss > 0 and int(parsed.notna().sum()) >= max(3, int(0.70 * non_miss))
                if is_num:
                    med = statistics.median(parsed[parsed.notna()].tolist())
                    fill_val = f"{med:.2f}" if (med % 1) else str(round(med))
                else:
                    fill_val = "Unknown"
                work.loc[missing, col] = fill_val
                cells_modified += cnt
                log_lines.append(f"[missing_values:{col}] Filled {cnt} missing cells in '{col}' with '{fill_val}'.")

        # 5. Casing and spelling
        if f"casing_spelling:{col}" in accepted_ids:
            cmap = _casing_and_typo_map(work[col])
            if cmap:
                hit = work[col].isin(cmap)
                cnt = int(hit.sum())
                work.loc[hit, col] = work.loc[hit, col].map(cmap)
                cells_modified += cnt
                log_lines.append(f"[casing_spelling:{col}] Normalised {cnt} cells in '{col}'.")

        # 6. Outliers
        if f"outliers:{col}" in accepted_ids:
            parsed, _ = _numeric_parse(work[col])
            pos_vals = parsed[parsed.notna() & (parsed > 0)].tolist()
            if len(pos_vals) >= 12:
                med = statistics.median(pos_vals)
                mad = statistics.median([abs(x - med) for x in pos_vals])
                sigma = 1.4826 * mad if mad > 0 else max(abs(med) * 0.5, 1.0)
                upper = med + 8.0 * sigma
                out_mask = parsed.notna() & (parsed > upper) & (parsed > med * 5)
                cnt = int(out_mask.sum())
                if cnt:
                    cap_str = f"{upper:.2f}" if PRICE_COL_RX.search(col) else str(round(upper))
                    work.loc[out_mask, col] = cap_str
                    cells_modified += cnt
                    log_lines.append(f"[outliers:{col}] Capped {cnt} extreme outliers in '{col}' at {cap_str}.")

    csv_text = ""
    neutralised = 0
    if not preview_only:
        csv_text, neutralised = to_safe_csv(work)
        if neutralised:
            log_lines.append(f"[security] Neutralised {neutralised} spreadsheet formula-injection cells (prefixed with ').")
    log_lines.append(f"Final dataset: {len(work)} rows ({input_rows - len(work)} rows removed, {cells_modified} cells updated).")

    return {
        "input_rows": input_rows,
        "output_rows": len(work),
        "rows_removed": input_rows - len(work),
        "cells_modified": cells_modified,
        "formula_cells_neutralised": neutralised,
        "columns": list(work.columns),
        "preview_after": work.head(10).to_dict(orient="records"),
        "changelog": "\n".join(log_lines) + "\n",
        "cleaned_csv": csv_text,
    }


def build_sample_messy_csv() -> str:
    """Build a realistic messy CSV from real Online Retail II transaction rows."""
    base = pd.read_parquet(
        LINES,
        columns=["invoice", "stock_code", "description", "quantity", "invoice_date", "unit_price", "customer_id", "country"],
    )
    uk = base[base["country"] == "United Kingdom"].head(35).copy()
    de = base[base["country"] == "Germany"].head(10).copy()
    sample = pd.concat([uk, de], ignore_index=True)
    out = pd.DataFrame({
        "InvoiceNo": sample["invoice"].astype(str),
        "StockCode": sample["stock_code"].astype(str),
        "Description": sample["description"].astype(str),
        "Quantity": sample["quantity"].astype(str),
        "InvoiceDate": sample["invoice_date"].astype(str),
        "UnitPrice": sample["unit_price"].map(lambda x: f"{float(x):.2f}"),
        "CustomerID": sample["customer_id"].fillna("").astype(str),
        "Country": sample["country"].astype(str),
    })

    # Real messy anomalies modeled on raw retail feed issues (duplicates, missing IDs, casing/typos,
    # mixed unit text, zero/negative price, extreme wholesale quantity, and formula-prefixed note)
    out.loc[2, "Country"] = "  united kingdom "
    out.loc[5, "Country"] = "Untied Kingdom"
    out.loc[7, "Description"] = "  " + out.loc[7, "Description"].lower() + "  "
    out.loc[9, "CustomerID"] = ""
    out.loc[10, "CustomerID"] = "NA"
    out.loc[12, "Quantity"] = "12 pcs"
    out.loc[14, "UnitPrice"] = "0.00"
    out.loc[15, "UnitPrice"] = "-2.50"
    out.loc[18, "Quantity"] = "4800"
    out.loc[20, "Description"] = "=1+1 GIFT TAG PROMO"
    # Append two exact duplicate rows from real rows 0 and 1
    out = pd.concat([out, out.iloc[[0, 1]]], ignore_index=True)
    buf = io.StringIO()
    out.to_csv(buf, index=False, lineterminator="\n")
    return buf.getvalue()
