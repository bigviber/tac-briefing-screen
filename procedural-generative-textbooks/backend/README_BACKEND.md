# Axiom backend

Python 3.12+ (developed on 3.13). The binding interface contract is `../docs/ARCHITECTURE.md`.

## Install

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt      # or: pip install -e ".[dev]"
```

## Configuration

All settings come from `AXIOM_*` environment variables (see `axiom/config.py`):

| Variable | Default |
|---|---|
| `AXIOM_DATA_DIR` | `backend/data` (holds `axiom.db`, `cases/`, `research_cache/`) |
| `AXIOM_BENCHMARKS_DIR` | `axiom/engine/benchmarks` |
| `AXIOM_SANDBOX_TIMEOUT_S` / `AXIOM_SANDBOX_MEMORY_MB` | `20` / `512` |
| `AXIOM_SQL_ROW_LIMIT` | `500` |
| `AXIOM_CORS_ORIGINS` | `http://localhost:5173` (comma-separated or JSON list) |
| `AXIOM_SEC_USER_AGENT` | `Axiom local research tool contact@example.com` |

## Tests

```bash
cd backend
python -m pytest -q
```

Tests use the mini corpus in `tests/fixtures/benchmarks/` and never touch the network
(the EDGAR client is exercised through `httpx.MockTransport`).

## Research-tier CLI

```bash
python -m axiom.engine.market_research report                       # coverage + citations (JSON)
python -m axiom.engine.market_research sample saas.gross_churn_monthly --seed 7 -n 5
python -m axiom.engine.market_research refresh --year 2024          # SEC EDGAR XBRL frames, in memory only
python -m axiom.engine.market_research --dir tests/fixtures/benchmarks report
```

Add `-v` to see loader warnings (missing files, skipped invalid records). `refresh` caches
frames under `data/research_cache/`, stays under 5 requests/s, and never rewrites the
corpus JSON files. If the network fails, the corpus values stay in effect.
