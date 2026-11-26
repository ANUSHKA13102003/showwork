# Showwork

**Ask your data anything. It shows its work.**

[![CI](https://github.com/ANUSHKA13102003/showwork/actions/workflows/ci.yml/badge.svg)](https://github.com/ANUSHKA13102003/showwork/actions/workflows/ci.yml)

Showwork is a small, measured semantic-layer copilot over real UK giftware transactions from the UCI Online Retail II dataset (`2009-12-01` to `2011-12-09`, GBP, CC BY 4.0). The LLM never calculates, never writes a number by itself, and never executes SQL it wrote: Gemini maps questions to a validated metric request, read-only DuckDB computes every result, and clicking any figure opens its metric definition, SQL, and row count. On the held-out test split, Showwork reached **100.0% answer-value accuracy** (`14/14`, 95% CI `78.5%–100.0%`) vs **0.0%** (`0/14`, 95% CI `0.0%–21.5%`) for a plain-LLM baseline, and blocked **100.0%** (`6/6`, 95% CI `61.0%–100.0%`) of out-of-scope and prompt-injection attempts.

## What is inside

- **Ask (`/ask`)**: Plain-English questions compiled to governed DuckDB SQL, exact GBP driver breakdowns (`country`, `category`, `customer_type`), and clickable Show work inspection; falls back to a deterministic rule parser when no API key is set.
- **Scorecard (`/scorecard`)**: Held-out evaluation results with 95% Wilson confidence intervals, plain-LLM baseline comparison, prompt-injection outcomes, semantic metric dictionary, and data contract quarantine counts.

## Run locally

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate   |   macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python run_all.py
uvicorn app.main:app --reload
```

Optional: copy `.env.example` to `.env` and add `GEMINI_API_KEY` for LLM request translation; without a key, Showwork runs in deterministic rule-based mode.

## How it works

```text
Raw UCI Excel (1,067,371 rows)
  --> pipeline/clean.py (explicit data contract: 1,021,252 clean rows, 46,119 quarantined with reason codes)
  --> pipeline/categories.py (regex rules -> rapidfuzz -> batched temp-0 LLM -> 18 categories)
  --> data/lines.parquet + read-only DuckDB (sales, returns, lines)
  --> Question -> Gemini or rule parser -> Pydantic whitelist (pipeline/metrics.yml) -> parameterised SQL -> verified answer + Show work
```

## Evaluation results

Ground truth is computed independently in plain `pandas` (`evals/run.py`) across 40 questions (20 `dev`, 20 held-out `test`: 28 answerable, 8 refusals/clarifications, 4 prompt injections). Intervals are 95% Wilson score bounds.

| System / Mode | Split | Request-match accuracy | Answer-value accuracy | Refusal / injection accuracy |
| :--- | :--- | ---: | ---: | ---: |
| **Showwork (LLM + compiler)** | `dev` | 92.9% (13/14, CI 68.5–98.7%) | 100.0% (14/14, CI 78.5–100.0%) | 100.0% (6/6, CI 61.0–100.0%) |
| **Showwork (LLM + compiler)** | `test` | 92.9% (13/14, CI 68.5–98.7%) | 100.0% (14/14, CI 78.5–100.0%) | 100.0% (6/6, CI 61.0–100.0%) |
| **Showwork (No-key rule mode)** | `dev` | 100.0% (14/14, CI 78.5–100.0%) | 100.0% (14/14, CI 78.5–100.0%) | 100.0% (6/6, CI 61.0–100.0%) |
| **Showwork (No-key rule mode)** | `test` | 92.9% (13/14, CI 68.5–98.7%) | 92.9% (13/14, CI 68.5–98.7%) | 100.0% (6/6, CI 61.0–100.0%) |
| **Plain LLM (schema + 50 rows)** | `dev` | n/a | 7.1% (1/14, CI 1.3–31.5%) | 100.0% (6/6, CI 61.0–100.0%) |
| **Plain LLM (schema + 50 rows)** | `test` | n/a | 0.0% (0/14, CI 0.0–21.5%) | 100.0% (6/6, CI 61.0–100.0%) |

Recorded failures:
- `Showwork (No-key)` on `test_04` (*"How many customers placed orders in 2010?"*): keyword order matched `orders` (`19,627`) before `customers` (`4,205`). Showwork LLM mode answered `4,205` accurately.
- `Showwork (LLM)` on `dev_14` and `test_14`: returned exact values (`£1,299,552.88` and `£591,767.18`), but emitted `limit=1` on ungrouped scalar queries where the test harness checked `limit=20`.
- `Plain-LLM baseline`: guessed approximate totals or declined to aggregate across 1.02M rows from 50 sample rows (27 of 28 answerable questions missed).

## Data source and limitations

- **Source & licence**: UCI Machine Learning Repository, *Online Retail II* by Dr. Daqing Chen (`https://doi.org/10.24432/C5CG6D`), licensed under Creative Commons Attribution 4.0 (**CC BY 4.0**), fetched via the matching two-sheet Kaggle mirror (`lakshmi25npathi/online-retail-dataset`) with direct UCI archive fallback.
- **Categories are derived**: the raw dataset has no category column; categories come from product descriptions via rules, fuzzy matching, and cached LLM labels (`65.0%` agreement when checked against `120` hidden rule-labelled items — a consistency check, not ground truth).
- **"New customer" cohort boundary**: `customer_type = 'new'` means first seen in this dataset starting `2009-12-01`, which inflates new-customer counts in early months.
- **Single retailer & historical replay**: covers one UK wholesaler-heavy gift retailer (`2009–2011`), and weekly views replay that historical window.
- **Evaluation scope**: 40 self-authored questions; the plain-LLM baseline sees schema metadata and 50 sample rows rather than executing SQL.
- **Runtime limits**: free-tier Gemini rate limits apply, and the daily LLM call counter (`150` calls/day default) lives in process memory and resets when the container restarts.
