# Methods & Architecture

## 1. Source Data and Licence
Showwork uses the real **Online Retail II** dataset from the UCI Machine Learning Repository (Dr. Daqing Chen, London South Bank University; DOI `10.24432/C5CG6D`, licensed under **CC BY 4.0**), covering UK online giftware transactions between `2009-12-01` and `2011-12-09` in GBP.

## 2. Data Contract (`pipeline/clean.py`)
Every raw row (`1,067,371` across the `Year 2009-2010` and `Year 2010-2011` sheets) passes through explicit deterministic rules and lands in either the clean analytical dataset (`data/lines.parquet`) or `data/quarantine.parquet` with a reason code:

- `SHEET_OVERLAP`: `22,523` rows from `2010-12-01` to `2010-12-09` duplicated across both workbook sheets.
- `DUPLICATE_ROW`: `11,812` exact duplicate transaction lines.
- `PRICE_NOT_POSITIVE`: `5,961` rows with `UnitPrice <= 0`.
- `NON_PRODUCT_CODE`: `5,817` rows representing postage (`POST`, `DOT`), manual entries (`M`), discounts (`D`), bank charges, Amazon fees, test rows, or gift vouchers.
- `BAD_DEBT_ADJUSTMENT`: `6` accounting write-off invoices prefixed with `A`.
- `NEGATIVE_QTY_NOT_CANCELLED`, `CANCELLED_POSITIVE_QTY`, `DATE_OUT_OF_RANGE`, `MISSING_COUNTRY`, `MISSING_DESCRIPTION`: explicit contract guards recorded in `pipeline/results/quality.json`.

Valid cancellation invoices (starting with `C`) are tagged `kind = 'return'` and exposed both in `lines` and in the dedicated `returns` DuckDB table. Missing `Customer ID` values (`22.24%` of clean rows) are retained and flagged `is_guest = true` (`customer_type = 'guest'`). Where one stock code appears with multiple descriptions (`595` stock codes), all rows are normalised to the most frequent description on valid sale lines.

## 3. Category Normalisation (`pipeline/categories.py`)
Online Retail II has no product category column. Distinct canonical product descriptions (`4,737` stock codes) are mapped to 18 categories in three deterministic-first stages:
1. Ordered regex dictionary rules (`90.82%` of products).
2. `rapidfuzz` token typo recovery against the rule vocabulary (`0.19%`).
3. Batched `temperature=0` Gemini classification constrained to the allowed enum for remaining items (`8.99%`), cached in `pipeline/mappings/category_map.csv`. Items below `0.90` confidence go to `pipeline/mappings/review_queue.csv`.

Because neither rules nor the LLM are true ground truth, `consistency_check()` hides `120` rule-labelled products from the model and measures agreement (`66.7%`), recording disagreements such as multi-purpose giftware (`PANTRY HOOK BALLOON WHISK`: rule `Party` vs LLM `Kitchen and Dining`).

## 4. Semantic Compiler & Driver Breakdown (`app/compiler.py`, `app/drivers.py`)
`pipeline/metrics.yml` defines 9 metrics and 5 dimensions with SQL expressions, grain, owner, caveats, and version numbers. User questions are mapped (by Gemini in LLM mode or `app/parser.py` in no-key mode) to a `pydantic`-validated `Request`. `app/compiler.py` compiles parameterised SQL from whitelisted identifiers only and executes it on a `read_only=True` DuckDB connection with `enable_external_access=False`. LLM-generated SQL is never accepted or executed.

For `"Why did revenue change?"`, `app/drivers.py` decomposes additive metrics (`net_revenue`, `gross_revenue`, `returns`, `units`) across `country`, `category`, and `customer_type`, grouping the tail into `All other (N)` so every breakdown sums to the exact GBP change.

## 5. Evaluation & Plain-LLM Baseline (`evals/run.py`)
`evals/questions.yml` holds 40 questions (20 `dev`, 20 held-out `test`): 28 answerable, 8 refusals/clarifications, and 4 adversarial prompt injections. Ground truth is computed independently in plain `pandas` without importing the compiler or DuckDB. Each accuracy metric is reported with a 95% Wilson score confidence interval alongside a plain-LLM baseline given the schema and 50 sample rows.

## 6. Weekly Briefings & Numeric Verifier (`app/briefing.py`, `app/verifier.py`)
Weekly briefings replay the historical feed week by week. Anomaly flags compare the selected week against the prior 8 weeks using the median and median absolute deviation ($\text{MAD}$), flagging $|x - \text{median}| > 3 \times 1.4826 \times \text{MAD}$ while noting seasonal November peaks. When Gemini drafts the <=200-word narrative from the JSON facts, `app/verifier.py` extracts every numeric literal (`£1,200`, `1.2k`, `+14.2%`, counts) and verifies each against the computed fact sheet; any mismatch triggers one regeneration and then falls back to a deterministic template.
