"""Tests for axiom.engine.market_research (no network: httpx is mocked)."""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

import httpx
import numpy as np
import pytest
from scipy import stats

from axiom.engine.market_research import (
    BenchmarkPool,
    EdgarFramesAdapter,
    EdgarRefreshSpec,
    ResearchBrief,
    main,
)
from axiom.engine.rng import SeededRNG


# --------------------------------------------------------------------------- loading


def test_fixture_corpus_loads(pool: BenchmarkPool) -> None:
    assert len(pool) >= 12
    assert pool.load_issues == []
    assert pool.has("saas.gross_churn_monthly") and not pool.has("nope")
    with pytest.raises(KeyError):
        pool.get("nope")
    assert pool.industry_codes() == ["manufacturing", "saas"]
    saas = pool.industry("saas")
    assert "saas.gross_churn_monthly" in saas.benchmarks
    assert "saas.arpa_monthly_usd" in saas.benchmarks  # tagged with industry, not declared
    assert "saas.not_in_corpus" not in saas.benchmarks
    assert "Subscription revenue" in saas.line_items  # filled from line_items.json
    mfg = pool.industry("manufacturing")
    assert mfg.line_items[0] == "Net sales"  # profile's own list wins
    assert "any.current_ratio" in mfg.benchmarks
    assert [i.code for i in pool.industries()] == ["manufacturing", "saas"]
    with pytest.raises(KeyError):
        pool.industry("biotech")


def test_reference_material(pool: BenchmarkPool) -> None:
    li = pool.line_items("saas")
    assert set(li) == {"income_statement", "balance_sheet", "cash_flow"}
    sub = li["income_statement"][0]
    assert sub.caption == "Subscription revenue" and sub.typical_pct_of_revenue == {"p25": 0.85, "p50": 0.92, "p75": 0.97}
    assert li["income_statement"][2].typical_pct_of_revenue is None
    assert pool.line_items("retail")["balance_sheet"][0].caption == "Cash"  # falls back to "any"
    tmpl = pool.schema_template("subscription_billing")
    inv = tmpl.table("invoices")
    assert inv.foreign_keys == [("customer_id", "customers", "customer_id")]
    assert inv.typical_rows_per_month == 900
    assert pool.forensic_pattern("channel_stuffing").pillar == "accounting_finance"
    assert [p.key for p in pool.patterns_for_pillar("system_design")] == ["duplicate_webhook_delivery"]
    assert pool.patterns_for_pillar("python") == []
    cites = pool.citations()
    assert len(cites) == len(pool) and set(cites[0]) == {"key", "source", "source_url"}
    assert [c["key"] for c in cites] == sorted(c["key"] for c in cites)


def test_search(pool: BenchmarkPool) -> None:
    dso = pool.search(metric_substring="days sales")
    assert {b.key for b in dso} == {"saas.dso_days", "manufacturing.dso_days"}
    assert all(b.industry == "saas" for b in pool.search(industry="saas"))
    with_any = pool.search(industry="manufacturing", include_any=True)
    assert "any.order_fill_rate" in {b.key for b in with_any}


def test_coverage_report(pool: BenchmarkPool) -> None:
    rep = pool.coverage_report()
    assert rep["industries"]["saas"]["missing"] == ["saas.not_in_corpus"]
    assert "saas.gross_churn_monthly" in rep["industries"]["saas"]["present"]
    assert rep["industries"]["saas"]["schema_template_present"] is True
    assert rep["benchmarks_total"] == len(pool)
    assert rep["forensic_patterns_by_pillar"] == {"accounting_finance": 1, "system_design": 1}
    json.dumps(rep)


def _copy_fixture(src: Path, dst: Path) -> Path:
    shutil.copytree(src, dst)
    return dst


def test_missing_file_tolerated(fixture_benchmarks_dir: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    corpus = _copy_fixture(fixture_benchmarks_dir, tmp_path / "corpus")
    (corpus / "schema_templates.json").unlink()
    (corpus / "operations.json").unlink()
    with caplog.at_level(logging.WARNING, logger="axiom.market_research"):
        pool = BenchmarkPool.load(corpus)
    assert any("schema_templates.json" in r.getMessage() for r in caplog.records)
    assert pool.schema_template_keys() == []
    assert not pool.has("manufacturing.oee") and pool.has("saas.dso_days")
    assert len(pool.load_issues) == 2
    brief = ResearchBrief.for_archetype("cohort_collapse", "saas", pool)
    assert brief["schema_template"] is None and "schema_template:subscription_billing" in brief["missing"]
    empty = BenchmarkPool.load(tmp_path / "does_not_exist")
    assert len(empty) == 0 and len(empty.load_issues) == 7


def test_invalid_records_skipped(fixture_benchmarks_dir: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    corpus = _copy_fixture(fixture_benchmarks_dir, tmp_path / "corpus")
    path = corpus / "saas_metrics.json"
    doc = json.loads(path.read_text())
    n_valid = len(doc["benchmarks"])
    doc["benchmarks"] += [
        {"key": "saas.bad_order", "industry": "saas", "metric": "m", "unit": "x",
         "p10": 5, "p25": 4, "p50": 6, "p75": 7, "p90": 8, "source": "s"},          # non-monotone
        {"key": "saas.no_source", "industry": "saas", "metric": "m", "unit": "x",
         "p10": 1, "p25": 2, "p50": 3, "p75": 4, "p90": 5},                          # missing source
        "not an object",
        {"key": "saas.extra_ok", "industry": "saas", "metric": "m", "unit": "x", "p10": 1, "p25": 2,
         "p50": 3, "p75": 4, "p90": 5, "source": "s", "sample_size": 120},           # unknown key dropped
    ]
    path.write_text(json.dumps(doc))
    (corpus / "forensic_patterns.json").write_text(json.dumps({"patterns": [
        {"key": "x", "title": "X", "pillar": "not_a_pillar"},
        {"key": "y", "title": "Y", "pillar": "sql"},
    ]}))
    (corpus / "industries.json").write_text("{ this is not json")
    with caplog.at_level(logging.WARNING, logger="axiom.market_research"):
        pool = BenchmarkPool.load(corpus)
    assert not pool.has("saas.bad_order") and not pool.has("saas.no_source")
    assert pool.has("saas.extra_ok")
    assert len(pool.search(industry="saas")) == n_valid + 1
    assert pool.forensic_pattern_keys() == ["y"]
    assert pool.industry_codes() == []
    assert sum("invalid record skipped" in i for i in pool.load_issues) == 3
    assert any("unreadable" in i for i in pool.load_issues)
    assert any("invalid record skipped" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- sampling


def test_sample_within_tails(pool: BenchmarkPool) -> None:
    rng = SeededRNG(7)
    for b in pool.search():
        if b.distribution != "empirical":
            continue
        lo, hi = b.p10 - (b.p25 - b.p10), b.p90 + (b.p90 - b.p75)
        draws = [pool.sample(b.key, rng) for _ in range(500)]
        assert min(draws) >= lo - 1e-12 and max(draws) <= hi + 1e-12, b.key


def test_sample_deterministic_and_sample_many_order_independent(pool: BenchmarkPool) -> None:
    a = pool.sample("saas.dso_days", SeededRNG(1))
    assert a == pool.sample("saas.dso_days", SeededRNG(1))
    keys = ["saas.dso_days", "saas.gross_margin", "manufacturing.oee"]
    m1 = pool.sample_many(keys, SeededRNG(5))
    m2 = pool.sample_many(list(reversed(keys)) + ["any.current_ratio"], SeededRNG(5))
    assert all(m1[k] == m2[k] for k in keys)


@pytest.mark.parametrize("rho", [-0.7, 0.7])
def test_copula_preserves_correlation_sign(pool: BenchmarkPool, rho: float) -> None:
    keys = ["saas.gross_churn_monthly", "saas.net_revenue_retention", "saas.cac_payback_months"]
    corr = np.array([[1.0, rho, 0.0], [rho, 1.0, 0.0], [0.0, 0.0, 1.0]])
    out = pool.correlated_sample(keys, SeededRNG(2024), corr, n=5000)
    assert all(len(v) == 5000 for v in out.values())
    r = stats.spearmanr(out[keys[0]], out[keys[1]]).statistic
    assert np.sign(r) == np.sign(rho) and abs(r) > 0.5
    assert abs(stats.spearmanr(out[keys[0]], out[keys[2]]).statistic) < 0.05
    # marginals still follow each benchmark
    b = pool.get(keys[0])
    assert np.median(out[keys[0]]) == pytest.approx(b.p50, rel=0.05)
    single = pool.correlated_sample(keys, SeededRNG(1), corr)
    assert set(single) == set(keys) and all(isinstance(v, float) for v in single.values())


def test_copula_validates_and_repairs_matrix(pool: BenchmarkPool) -> None:
    keys = ["saas.dso_days", "saas.gross_margin", "manufacturing.oee"]
    with pytest.raises(ValueError):
        pool.correlated_sample(keys, SeededRNG(1), np.eye(2))
    with pytest.raises(ValueError):
        pool.correlated_sample(keys, SeededRNG(1), [[1, 0.5, 0], [0.4, 1, 0], [0, 0, 1]])
    not_pd = [[1, 0.9, -0.9], [0.9, 1, 0.9], [-0.9, 0.9, 1]]
    out = pool.correlated_sample(keys, SeededRNG(1), not_pd, n=100)
    assert all(np.all(np.isfinite(v)) for v in out.values())


# --------------------------------------------------------------------------- research brief


def test_research_brief(pool: BenchmarkPool) -> None:
    brief = ResearchBrief.for_archetype("idempotent_webhooks", "saas", pool)
    assert brief["industry"]["code"] == "saas"
    assert [p["key"] for p in brief["forensic_patterns"]] == ["duplicate_webhook_delivery"]
    assert brief["schema_template"]["key"] == "subscription_billing"
    assert set(brief["benchmark_keys"]) >= set(pool.industry("saas").benchmarks)
    assert brief["missing"] == ["saas.not_in_corpus"]
    assert set(brief["line_items"]) == {"income_statement", "balance_sheet", "cash_flow"}
    assert {c["key"] for c in brief["citations"]} == set(brief["benchmark_keys"])
    json.dumps(brief)

    wc = ResearchBrief.for_archetype("working_capital_crunch", "manufacturing", pool)
    assert "any.current_ratio" in wc["benchmark_keys"] and "manufacturing.dso_days" in wc["benchmark_keys"]
    assert [p["key"] for p in wc["forensic_patterns"]] == ["channel_stuffing"]

    unknown = ResearchBrief.for_archetype("working_capital_crunch", "retail", pool)
    assert unknown["industry"]["code"] == "retail" and unknown["line_items"]["income_statement"]
    assert ResearchBrief.for_archetype("not_registered", "saas", pool)["pillars"]


def test_real_corpus_loads_without_crashing() -> None:
    pool = BenchmarkPool.load()  # whatever the researcher has authored so far
    pool.coverage_report()
    for code in pool.industry_codes():
        ResearchBrief.for_archetype("working_capital_crunch", code, pool)


# --------------------------------------------------------------------------- EDGAR


def _frame(concept: str, period: str, values: dict[int, float]) -> dict:
    return {
        "taxonomy": "us-gaap", "tag": concept, "ccp": period, "uom": "USD", "pts": len(values),
        "data": [{"accn": f"0000-{cik}", "cik": cik, "entityName": f"Co {cik}", "loc": "US-NY",
                  "end": "2024-12-31", "val": v} for cik, v in values.items()],
    }


@pytest.fixture()
def frames() -> dict[str, dict]:
    rng = np.random.default_rng(0)
    ciks = list(range(1000, 1060))
    revenue = {c: float(rng.uniform(1e7, 1e9)) for c in ciks}
    margins = {c: float(rng.uniform(0.1, 0.7)) for c in ciks}
    gross = {c: revenue[c] * margins[c] for c in ciks[:50]}  # 50 filers report both
    gross[9999] = 1.0  # filer without revenue is ignored
    revenue[1001] = 0.0  # zero denominator is ignored (it is also in gross)
    return {
        "/api/xbrl/frames/us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax/USD/CY2024.json":
            _frame("RevenueFromContractWithCustomerExcludingAssessedTax", "CY2024", revenue),
        "/api/xbrl/frames/us-gaap/GrossProfit/USD/CY2024.json": _frame("GrossProfit", "CY2024", gross),
        "_margins": {"data": [margins[c] for c in ciks[:50] if c != 1001]},
    }


def _transport(frames: dict[str, dict], seen: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = frames.get(request.url.path)
        if body is None:
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


def test_edgar_refresh_computes_percentiles_and_caches(frames: dict[str, dict], tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    cache = tmp_path / "research_cache"
    adapter = EdgarFramesAdapter(cache_dir=cache, transport=_transport(frames, seen), max_requests_per_second=1000)
    bench = adapter.refresh_benchmark("GrossProfit", "RevenueFromContractWithCustomerExcludingAssessedTax",
                                      2024, "edgar.any.gross_margin", "any", unit="ratio")
    assert bench is not None
    expected = np.percentile(frames["_margins"]["data"], [10, 25, 50, 75, 90])
    assert bench.percentiles() == pytest.approx(list(expected), rel=1e-9)
    assert bench.source == "SEC EDGAR XBRL frames" and bench.as_of == "2024"
    assert bench.source_url.startswith("https://data.sec.gov/api/xbrl/frames/us-gaap/GrossProfit/USD/CY2024")
    assert "49 filers" in bench.notes
    assert len(seen) == 2
    assert seen[0].headers["User-Agent"] == EdgarFramesAdapter.DEFAULT_USER_AGENT
    assert len(list(cache.glob("*.json"))) == 2
    adapter.close()

    # A fresh adapter whose network always fails still works from the cache.
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    cached = EdgarFramesAdapter(cache_dir=cache, transport=httpx.MockTransport(boom), max_requests_per_second=1000)
    again = cached.refresh_benchmark("GrossProfit", "RevenueFromContractWithCustomerExcludingAssessedTax",
                                     2024, "edgar.any.gross_margin")
    assert again is not None and again.p50 == bench.p50 and cached.network_requests == 0


def test_edgar_user_agent_env_override(monkeypatch: pytest.MonkeyPatch, frames: dict[str, dict], tmp_path: Path) -> None:
    monkeypatch.setenv("AXIOM_SEC_USER_AGENT", "Tester test@example.org")
    seen: list[httpx.Request] = []
    adapter = EdgarFramesAdapter(cache_dir=tmp_path, transport=_transport(frames, seen), max_requests_per_second=1000)
    adapter.fetch_frame("GrossProfit", "CY2024")
    assert seen[0].headers["User-Agent"] == "Tester test@example.org"


def test_edgar_failures_fall_back_to_corpus(pool: BenchmarkPool, frames: dict[str, dict], tmp_path: Path,
                                            caplog: pytest.LogCaptureFixture) -> None:
    seen: list[httpx.Request] = []
    adapter = EdgarFramesAdapter(cache_dir=tmp_path, transport=_transport(frames, seen), max_requests_per_second=1000)
    original = pool.get("saas.dso_days")
    specs = [
        EdgarRefreshSpec(key="saas.dso_days", industry="saas", metric="DSO", unit="days",
                         numerator="AccountsReceivableNetCurrent", numerator_instant=True,
                         denominator="RevenueFromContractWithCustomerExcludingAssessedTax", multiplier=365),  # 404
        EdgarRefreshSpec(key="saas.gross_margin", industry="saas", metric="Gross margin",
                         numerator="GrossProfit", denominator="RevenueFromContractWithCustomerExcludingAssessedTax"),
    ]
    corpus_file = Path(pool.source_dir) / "saas_metrics.json"  # type: ignore[arg-type]
    before = corpus_file.read_text()
    with caplog.at_level(logging.WARNING, logger="axiom.market_research"):
        applied = pool.apply_live_overrides(adapter, specs, year=2024)
    assert [b.key for b in applied] == ["saas.gross_margin"]
    assert pool.get("saas.dso_days") == original
    assert pool.get("saas.gross_margin").source == "SEC EDGAR XBRL frames"
    assert pool.coverage_report()["live_overrides"] == ["saas.gross_margin"]
    assert corpus_file.read_text() == before
    assert any("CY2024Q4I" in str(r.url) for r in seen)
    assert any("keeping corpus value" in r.getMessage() for r in caplog.records)
    pool.clear_overrides()
    assert pool.get("saas.gross_margin").source != "SEC EDGAR XBRL frames"

    few = EdgarFramesAdapter(cache_dir=tmp_path, transport=_transport(frames, []), min_filers=1000)
    assert few.refresh_from_spec(specs[1], 2024) is None


# --------------------------------------------------------------------------- CLI


def test_cli_report_and_sample(fixture_benchmarks_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--dir", str(fixture_benchmarks_dir), "report"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["coverage"]["benchmarks_total"] >= 12 and report["citations"]
    assert main(["--dir", str(fixture_benchmarks_dir), "sample", "saas.gross_churn_monthly", "--seed", "7"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert main(["--dir", str(fixture_benchmarks_dir), "sample", "saas.gross_churn_monthly", "--seed", "7"]) == 0
    assert json.loads(capsys.readouterr().out)["draws"] == first["draws"]
    assert main(["--dir", str(fixture_benchmarks_dir), "sample", "missing.key"]) == 2
