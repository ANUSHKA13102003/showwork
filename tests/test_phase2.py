from pathlib import Path

from fastapi.testclient import TestClient

from app import briefing, verifier
from app.main import app


def test_verifier_accepts_grounded_formats_and_rejects_hallucinations():
    facts = briefing.compute_facts("2011-11-28")
    tmpl = briefing.template_summary(facts)
    res_ok = verifier.verify(tmpl, facts)
    assert res_ok["ok"] is True

    rev = next(k for k in facts["kpis"] if k["key"] == "net_revenue")
    # Shorthand k format and exact percentage from facts
    k_short = f"£{rev['current'] / 1000:.1f}k"
    grounded = f"In the week of 2011-11-28, net revenue reached {k_short} ({rev['wow_pct_fmt']} vs the prior 4-week window)."
    assert verifier.verify(grounded, facts)["ok"] is True

    # Hallucinated figure must fail verification
    bad = "In the week of 2011-11-28, net revenue jumped to £9,876,543 (+88.4% week over week)."
    res_bad = verifier.verify(bad, facts)
    assert res_bad["ok"] is False
    assert len(res_bad["unmatched"]) >= 1

    # Word limit (> 200 words) must fail verification
    too_long = (" ".join(["word"] * 205)) + f" {rev['current_fmt']}"
    assert verifier.verify(too_long, facts)["ok"] is False


def test_verifier_fallback_on_bad_llm_output(monkeypatch):
    facts = briefing.compute_facts("2011-11-21")
    monkeypatch.setattr("app.llm.has_key", lambda: True)
    monkeypatch.setattr("app.llm.generate", lambda *a, **kw: "Net revenue was £99,999,999 (+91.3%).")
    summary = briefing.build_summary(facts, use_llm=True)
    assert summary["first_pass_failed"] is True
    assert summary["fallback_used"] is True
    assert summary["source"] == "template_fallback"
    assert summary["verification"]["ok"] is True


def test_briefing_november_note_and_driver_sums(tmp_path: Path):
    doc = briefing.generate_week("2011-11-14", out_dir=tmp_path, use_llm=False)
    assert doc["seasonal_note"] is not None
    assert "November" in doc["seasonal_note"]
    assert 3 <= len(doc["watchlist"]) <= 5
    for dim in ("country", "category", "customer_type"):
        assert doc["drivers"]["dimensions"][dim]["sums_to_total"] is True
    assert (tmp_path / "2011-11-14.json").exists()
    assert (tmp_path / "2011-11-14.md").exists()


def test_briefing_api_endpoints():
    client = TestClient(app)
    r = client.get("/api/briefings")
    assert r.status_code == 200
    weeks = r.json()["weeks"]
    assert len(weeks) >= 8
    w0 = weeks[0]["week"]
    r2 = client.get(f"/api/briefing/{w0}")
    assert r2.status_code == 200
    body = r2.json()
    assert body["week"] == w0
    assert len(body["kpis"]) == 6
    assert body["summary"]["verification"]["ok"] is True
