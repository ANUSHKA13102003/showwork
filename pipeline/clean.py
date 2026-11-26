import re

import numpy as np
import pandas as pd

PRODUCT_CODE = re.compile(r"^(\d{5}[A-Z]{0,3}|DCGS[A-Z0-9]+)$")
# Codes that look like products to a regex but are fees, postage or manual entries.
NON_PRODUCT_EXPLICIT = {"POST", "DOT", "M", "C2", "C3", "D", "S", "B", "BANK CHARGES", "ADJUST", "ADJUST2",
                        "AMAZONFEE", "PADS", "CRUK", "TEST001", "TEST002", "GIFT"}
OVERLAP_START = pd.Timestamp("2010-12-01")
DATA_START, DATA_END = pd.Timestamp("2009-12-01"), pd.Timestamp("2011-12-10")

REASONS = {
    "SHEET_OVERLAP": "Row repeated across the two source sheets (1 to 9 Dec 2010 appears in both).",
    "DUPLICATE_ROW": "Exact duplicate of an earlier row.",
    "NON_PRODUCT_CODE": "Stock code is postage, a fee, a manual adjustment, a test or a gift voucher.",
    "BAD_DEBT_ADJUSTMENT": "Invoice starting with A: an accounting adjustment, not a sale.",
    "MISSING_DESCRIPTION": "No product description and no other description for this stock code.",
    "PRICE_NOT_POSITIVE": "Unit price is zero or negative on a non-cancellation line.",
    "NEGATIVE_QTY_NOT_CANCELLED": "Negative quantity on an invoice that is not a cancellation (stock write-off).",
    "CANCELLED_POSITIVE_QTY": "Cancellation invoice with a positive quantity.",
    "DATE_OUT_OF_RANGE": "Invoice date outside 2009-12-01 to 2011-12-09.",
    "MISSING_COUNTRY": "No country recorded.",
}


def norm_text(s):
    return s.astype("string").str.upper().str.replace(r"\s+", " ", regex=True).str.strip().str.strip(".,")


def standardise(raw):
    df = pd.DataFrame({
        "invoice": raw["Invoice"].astype("string").str.strip().str.upper(),
        "stock_code_raw": raw["StockCode"].astype("string").str.strip(),
        "description_raw": raw["Description"].astype("string"),
        "quantity": raw["Quantity"].astype("int64"),
        "invoice_ts": pd.to_datetime(raw["InvoiceDate"]),
        "unit_price": raw["Price"].astype("float64"),
        "customer_id": raw["Customer ID"],
        "country": raw["Country"].astype("string").str.strip(),
        "source_sheet": raw["source_sheet"].astype("string"),
    })
    df["stock_code"] = df["stock_code_raw"].str.upper()
    df["description"] = norm_text(df["description_raw"])
    df.loc[df["description"] == "", "description"] = pd.NA
    df["customer_id"] = df["customer_id"].astype("Int64").astype("string")
    return df


def apply_contract(raw):
    """Return (sales_and_returns, quarantine, counts). Every input row ends up in exactly one output."""
    df = standardise(raw)
    df["reason"] = pd.Series(pd.NA, index=df.index, dtype="string")

    def mark(mask, code):
        hit = mask & df["reason"].isna()
        df.loc[hit, "reason"] = code

    mark((df["source_sheet"] == "Year 2009-2010") & (df["invoice_ts"] >= OVERLAP_START), "SHEET_OVERLAP")
    key = ["invoice", "stock_code_raw", "description_raw", "quantity", "invoice_ts", "unit_price", "customer_id", "country"]
    live = df["reason"].isna()
    dup = pd.Series(False, index=df.index)
    dup[live] = df.loc[live, key].duplicated(keep="first")
    mark(dup, "DUPLICATE_ROW")
    mark(df["invoice"].str.startswith("A"), "BAD_DEBT_ADJUSTMENT")
    code = df["stock_code"]
    nonprod = code.isin(NON_PRODUCT_EXPLICIT) | code.str.startswith("GIFT_") | ~code.str.match(PRODUCT_CODE.pattern)
    mark(nonprod, "NON_PRODUCT_CODE")

    is_cancel = df["invoice"].str.startswith("C")
    mark(~is_cancel & (df["unit_price"] <= 0), "PRICE_NOT_POSITIVE")
    mark(~is_cancel & (df["quantity"] <= 0), "NEGATIVE_QTY_NOT_CANCELLED")
    mark(is_cancel & (df["quantity"] > 0), "CANCELLED_POSITIVE_QTY")
    mark(is_cancel & (df["unit_price"] <= 0), "PRICE_NOT_POSITIVE")
    mark((df["invoice_ts"] < DATA_START) | (df["invoice_ts"] >= DATA_END), "DATE_OUT_OF_RANGE")
    mark(df["country"].isna() | (df["country"] == ""), "MISSING_COUNTRY")

    # One stock code, several descriptions: use the most frequent description on valid sale lines.
    ok = df["reason"].isna()
    sales_desc = df[ok & ~is_cancel & df["description"].notna()]
    canon = (sales_desc.groupby(["stock_code", "description"]).size().rename("n").reset_index()
             .sort_values(["stock_code", "n", "description"], ascending=[True, False, True])
             .drop_duplicates("stock_code").set_index("stock_code")["description"])
    any_desc = (df[ok & df["description"].notna()].groupby("stock_code")["description"]
                .agg(lambda s: s.value_counts().index[0]))
    canon = canon.combine_first(any_desc)
    df["description_canonical"] = df["stock_code"].map(canon).astype("string")
    mark(df["description_canonical"].isna(), "MISSING_DESCRIPTION")

    multi_desc_codes = int((df[df["reason"].isna()].groupby("stock_code")["description"].nunique() > 1).sum())
    ws_or_case = int((df["description_raw"].notna() & (df["description_raw"] != df["description"])
                      & df["reason"].isna()).sum())
    replaced = int((df["reason"].isna() & df["description"].notna()
                    & (df["description"] != df["description_canonical"])).sum())

    quarantine = df[df["reason"].notna()].copy()
    clean = df[df["reason"].isna()].copy()
    clean["kind"] = np.where(clean["invoice"].str.startswith("C"), "return", "sale")
    clean["is_guest"] = clean["customer_id"].isna()

    counts = {
        "input_rows": len(df),
        "clean_rows": len(clean),
        "sale_rows": int((clean["kind"] == "sale").sum()),
        "return_rows": int((clean["kind"] == "return").sum()),
        "quarantined_rows": len(quarantine),
        "quarantine_by_reason": {r: int((quarantine["reason"] == r).sum()) for r in REASONS},
        "reason_descriptions": REASONS,
        "flags": {
            "guest_rows": int(clean["is_guest"].sum()),
            "guest_share_of_rows": round(float(clean["is_guest"].mean()), 4),
            "unspecified_country_rows": int((clean["country"] == "Unspecified").sum()),
        },
        "descriptions": {
            "rows_with_whitespace_or_case_fixed": ws_or_case,
            "stock_codes_with_several_descriptions": multi_desc_codes,
            "rows_relabelled_to_canonical_description": replaced,
            "stock_codes_lowercase_in_source": int((df["stock_code_raw"] != df["stock_code"]).sum()),
        },
    }
    assert counts["clean_rows"] + counts["quarantined_rows"] == counts["input_rows"]
    return clean, quarantine, counts


def add_customer_type(clean):
    clean = clean.sort_values(["invoice_ts", "invoice"]).reset_index(drop=True)
    sales = clean[(clean["kind"] == "sale") & ~clean["is_guest"]]
    first = (sales.sort_values(["invoice_ts", "invoice"]).drop_duplicates("customer_id")
             .set_index("customer_id")["invoice"])
    ctype = pd.Series("returning", index=clean.index, dtype="string")
    ctype[clean["is_guest"]] = "guest"
    is_sale = clean["kind"] == "sale"
    ctype[is_sale & ~clean["is_guest"] & (clean["invoice"] == clean["customer_id"].map(first))] = "new"

    # A return takes the type of the customer's latest order at or before it; with no earlier
    # order in the data the purchase happened before December 2009, so it counts as returning.
    rets = clean[(clean["kind"] == "return") & ~clean["is_guest"]][["invoice_ts", "customer_id"]].reset_index()
    orders = clean[is_sale & ~clean["is_guest"]][["invoice_ts", "customer_id"]].copy()
    orders["otype"] = ctype[orders.index]
    orders = orders.drop_duplicates(["customer_id", "invoice_ts"], keep="last").sort_values("invoice_ts")
    if len(rets):
        m = pd.merge_asof(rets.sort_values("invoice_ts"), orders, on="invoice_ts", by="customer_id",
                          direction="backward")
        ctype[m["index"].to_numpy()] = m["otype"].fillna("returning").to_numpy()
    clean["customer_type"] = ctype
    return clean
