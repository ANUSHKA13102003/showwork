import io
import json

from fastapi.testclient import TestClient

from app import cleaner
from app.main import app


def test_formula_injection_neutraliser():
    assert cleaner.neutralise_cell("=CMD|' /C calc'!A0") == "'=CMD|' /C calc'!A0"
    assert cleaner.neutralise_cell("+SUM(A1:A10)") == "'+SUM(A1:A10)"
    assert cleaner.neutralise_cell("@SUM(1,2)") == "'@SUM(1,2)"
    assert cleaner.neutralise_cell("-1+1") == "'-1+1"
    assert cleaner.neutralise_cell("  =HYPERLINK('http://x')") == "'  =HYPERLINK('http://x')"
    # Genuine negative numbers remain plain numbers
    assert cleaner.neutralise_cell("-12.50") == "-12.50"
    assert cleaner.neutralise_cell("United Kingdom") == "United Kingdom"


def test_upload_size_and_extension_limits():
    client = TestClient(app)

    # Reject non-.csv extension
    r_type = client.post(
        "/api/clean/inspect",
        files={"file": ("data.xlsx", io.BytesIO(b"a,b\n1,2\n"), "application/vnd.ms-excel")},
    )
    assert r_type.status_code == 400

    # Reject > 5 MB CSV
    big = b"col1,col2\n" + (b"12345,67890\n" * 460_000)
    assert len(big) > cleaner.MAX_CSV_BYTES
    r_big = client.post(
        "/api/clean/inspect",
        files={"file": ("huge.csv", io.BytesIO(big), "text/csv")},
    )
    assert r_big.status_code == 413


def test_each_deterministic_fix_and_sample_endpoint():
    client = TestClient(app)
    r_sample = client.get("/api/clean/sample")
    assert r_sample.status_code == 200
    csv_text = r_sample.json()["csv"]

    r_ins = client.post(
        "/api/clean/inspect",
        files={"file": ("sample_messy.csv", io.BytesIO(csv_text.encode("utf-8")), "text/csv")},
    )
    assert r_ins.status_code == 200
    ins = r_ins.json()
    kinds = {item["kind"] for item in ins["issues"]}
    assert "duplicates" in kinds
    assert "missing_values" in kinds
    assert "mixed_types" in kinds
    assert "inconsistent_casing_and_spelling" in kinds
    assert "impossible_values" in kinds
    assert "outliers" in kinds

    all_ids = [item["id"] for item in ins["issues"]]
    r_apply = client.post(
        "/api/clean/apply",
        files={"file": ("sample_messy.csv", io.BytesIO(csv_text.encode("utf-8")), "text/csv")},
        data={"accepted_ids": json.dumps(all_ids), "preview_only": "0"},
    )
    assert r_apply.status_code == 200
    res = r_apply.json()
    assert res["rows_removed"] == 4  # 2 duplicates + 2 impossible UnitPrice <= 0 rows
    assert res["output_rows"] == res["input_rows"] - 4
    assert res["cells_modified"] > 0
    assert res["formula_cells_neutralised"] >= 1
    assert "'=1+1 GIFT TAG PROMO" in res["cleaned_csv"]
    assert "Untied Kingdom" not in res["cleaned_csv"]
    assert "[duplicates]" in res["changelog"]
    assert "[security]" in res["changelog"]
