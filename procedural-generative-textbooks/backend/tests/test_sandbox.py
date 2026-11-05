"""SQL and Python sandbox tests (tmp DuckDB, no network)."""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import duckdb
import pytest

from axiom.engine.sandbox import (
    OUTPUT_LIMIT_BYTES,
    PythonSandbox,
    SandboxRejected,
    SQLSandbox,
    json_safe,
    truncate_text,
    validate_sql,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX rlimits required")


@pytest.fixture()
def case_dir(tmp_path: Path) -> Path:
    """A case folder with a small company.duckdb (``x``: 2000 rows, ``y``: 3 rows)."""
    d = tmp_path / "case_1"
    d.mkdir()
    con = duckdb.connect(str(d / "company.duckdb"))
    con.execute(
        "CREATE TABLE x AS SELECT range AS i, (range * 1.5)::DECIMAL(10,2) AS amount, "
        "DATE '2024-01-01' + range::INTEGER AS d, 'r' || range AS label, "
        "CASE WHEN range % 7 = 0 THEN 'NaN'::DOUBLE ELSE range / 3.0 END AS ratio FROM range(2000)"
    )
    con.execute("CREATE TABLE y (k VARCHAR, payload BLOB, ts TIMESTAMP)")
    con.execute("INSERT INTO y VALUES ('a', '\\x01\\x02'::BLOB, TIMESTAMP '2024-05-01 10:00:00'), ('b', NULL, NULL), ('c', NULL, NULL)")
    con.close()
    return d


@pytest.fixture()
def sql(case_dir: Path) -> SQLSandbox:
    return SQLSandbox(case_dir / "company.duckdb", timeout_s=5)


# --------------------------------------------------------------------------- SQL allowlist


@pytest.mark.parametrize(
    "stmt",
    [
        "DROP TABLE x",
        "DELETE FROM x",
        "INSERT INTO x VALUES (1)",
        "UPDATE x SET i = 1",
        "CREATE TABLE z AS SELECT 1",
        "ATTACH 'other.duckdb' AS o",
        "COPY x TO 'out.csv'",
        "INSTALL httpfs",
        "LOAD httpfs",
        "EXPORT DATABASE 'dir'",
        "CALL pragma_version()",
        "SET threads = 8",
        "PRAGMA version",
        "SELECT 1; SELECT 2",
        "SELECT 1; DROP TABLE x",
        "WITH a AS (SELECT 1) INSERT INTO x SELECT * FROM a",
        "SELECT * FROM x; ATTACH 'f'",
        "",
        "   ;  ",
    ],
)
def test_allowlist_rejects(sql: SQLSandbox, stmt: str) -> None:
    with pytest.raises(SandboxRejected):
        sql.run(stmt)


def test_allowlist_accepts_read_statements() -> None:
    assert validate_sql("SELECT 1;") == "SELECT 1"
    assert validate_sql("  with a as (select 1) select * from a ;; ") == "with a as (select 1) select * from a"
    assert validate_sql("-- comment\nSELECT 'drop table; delete' AS s")
    assert validate_sql("PRAGMA table_info('x')")
    for s in ("DESCRIBE x", "SHOW TABLES", "EXPLAIN SELECT 1"):
        assert validate_sql(s) == s


def test_file_access_is_disabled(sql: SQLSandbox) -> None:
    out = sql.run("SELECT * FROM read_csv('/etc/passwd')")
    assert out["error"] and "disabled" in out["error"].lower()


# --------------------------------------------------------------------------- SQL results


def test_limit_and_truncated_exact(sql: SQLSandbox) -> None:
    out = sql.run("SELECT i FROM x ORDER BY i DESC", limit=10)
    assert out["error"] is None
    assert out["row_count"] == 10 and out["truncated"] is True
    assert [r[0] for r in out["rows"]] == list(range(1999, 1989, -1))
    exact = sql.run("SELECT i FROM x WHERE i < 10", limit=10)
    assert exact["row_count"] == 10 and exact["truncated"] is False
    under = sql.run("SELECT i FROM x WHERE i < 9", limit=10)
    assert under["row_count"] == 9 and under["truncated"] is False


def test_column_types_and_json_safe_cells(sql: SQLSandbox) -> None:
    out = sql.run("SELECT i, amount, d, label, ratio FROM x ORDER BY i LIMIT 2")
    assert out["columns"] == ["i", "amount", "d", "label", "ratio"]
    assert out["column_types"] == ["BIGINT", "DECIMAL(10,2)", "DATE", "VARCHAR", "DOUBLE"]
    assert out["rows"][0] == [0, 0.0, "2024-01-01", "r0", None]  # NaN -> null
    assert out["rows"][1][1] == 1.5 and isinstance(out["rows"][1][1], float)
    y = sql.run("SELECT * FROM y ORDER BY k")
    assert y["rows"][0][1] == base64.b64encode(b"\x01\x02").decode()
    assert y["rows"][0][2] == "2024-05-01T10:00:00"


def test_timestamptz_cells(sql: SQLSandbox) -> None:
    """TIMESTAMPTZ results (the ``tz_mixed`` dirt fix: ``CAST(col AS TIMESTAMPTZ)``) need ``pytz`` in DuckDB's
    Python conversion; without it every such query errors with "Required module 'pytz' failed to import"."""
    import datetime as dt

    out = sql.run("SELECT CAST('2024-03-31 23:30:00-05:00' AS TIMESTAMPTZ) AS t, "
                  "CAST('2024-03-31 23:30:00-05:00' AS TIMESTAMPTZ) AT TIME ZONE 'UTC' AS u FROM y LIMIT 1")
    assert out["error"] is None, out["error"]
    assert out["column_types"] == ["TIMESTAMP WITH TIME ZONE", "TIMESTAMP"]
    t, u = out["rows"][0]
    assert dt.datetime.fromisoformat(t) == dt.datetime(2024, 4, 1, 4, 30, tzinfo=dt.timezone.utc)  # same instant, any session TZ
    assert u == "2024-04-01T04:30:00"
    ref = sql.run_reference("SELECT CAST('2024-06-30 22:00:00+02:00' AS TIMESTAMPTZ) AS t")
    assert dt.datetime.fromisoformat(ref["first_row"]["t"]) == dt.datetime(2024, 6, 30, 20, 0, tzinfo=dt.timezone.utc)


def test_show_describe_pragma(sql: SQLSandbox) -> None:
    assert sorted(r[0] for r in sql.run("SHOW TABLES")["rows"]) == ["x", "y"]
    assert sql.run("DESCRIBE x")["row_count"] == 5
    info = sql.run("PRAGMA table_info('y')")
    assert info["error"] is None and info["row_count"] == 3


def test_user_error_is_returned_not_raised(sql: SQLSandbox) -> None:
    out = sql.run("SELECT nope FROM x")
    assert out["error"] and "nope" in out["error"] and out["rows"] == []


def test_timeout_interrupts_query(case_dir: Path) -> None:
    sb = SQLSandbox(case_dir / "company.duckdb", timeout_s=0.5)
    out = sb.run("SELECT count(*) FROM range(1000000000) a, range(1000000000) b")
    assert out["error"] and "timed out" in out["error"]
    assert out["elapsed_ms"] < 10_000
    assert sb.run("SELECT 42 AS v")["rows"] == [[42]]  # the sandbox is still usable


def test_run_reference_first_row(sql: SQLSandbox) -> None:
    ref = sql.run_reference("SELECT count(*) AS n, sum(amount) AS total FROM x;")
    assert ref["first_row"] == {"n": 2000, "total": pytest.approx(2998500.0)}
    assert ref["rows"] == [ref["first_row"]]


def test_missing_db(tmp_path: Path) -> None:
    out = SQLSandbox(tmp_path / "missing.duckdb").run("SELECT 1")
    assert out["error"] and "not found" in out["error"]


def test_helpers() -> None:
    assert json_safe(float("inf")) is None
    assert json_safe({"a": (1, 2)}) == {"a": [1, 2]}
    long = "x" * (OUTPUT_LIMIT_BYTES + 50)
    t = truncate_text(long)
    assert t.startswith("x" * 100) and "truncated: 50 more bytes" in t


# --------------------------------------------------------------------------- Python


@pytest.fixture()
def py(case_dir: Path) -> PythonSandbox:
    return PythonSandbox(case_dir, timeout_s=30, memory_mb=256)


def test_python_result_dict_and_stdout(py: PythonSandbox) -> None:
    out = py.run("print('hello'); import sys; print('warn', file=sys.stderr)\nresult = {'a': np.int64(3), 'b': [1.5, float('nan')], 'c': 'x'}")
    assert out["error"] is None, out
    assert out["stdout"] == "hello\n" and "warn" in out["stderr"]
    assert out["result_json"] == {"a": 3, "b": [1.5, None], "c": "x"}
    assert out["figures"] == [] and out["elapsed_ms"] > 0


def test_python_dataframe_result_and_lazy_tables(py: PythonSandbox) -> None:
    code = (
        "assert set(tables) == {'x', 'y'}\n"
        "assert 'loaded: []' in repr(tables)\n"
        "df = tables['x']\n"
        "print(len(df), type(df).__name__)\n"
        "assert 'loaded: [\\'x\\']' in repr(tables)\n"
        "result = df[['i', 'label']]\n"
    )
    out = py.run(code)
    assert out["error"] is None, out["stderr"]
    assert out["stdout"].strip() == "2000 DataFrame"
    rj = out["result_json"]
    assert rj["columns"] == ["i", "label"] and rj["row_count"] == 2000 and rj["truncated"] is True
    assert len(rj["rows"]) == 500 and rj["rows"][1] == [1, "r1"]


def test_python_con_is_read_only(py: PythonSandbox) -> None:
    out = py.run("con.execute('CREATE TABLE z (i INT)')")
    assert out["error"] and "read-only" in out["error"].lower()
    out2 = py.run("result = con.execute('SELECT count(*) FROM y').fetchone()[0]")
    assert out2["result_json"] == 3


def test_python_figure_capture(py: PythonSandbox) -> None:
    out = py.run("import matplotlib.pyplot as plt\nplt.plot([1, 2, 3], [3, 1, 2])\nplt.figure()\nplt.bar(['a'], [1])")
    assert out["error"] is None, out["stderr"]
    assert len(out["figures"]) == 2
    assert base64.b64decode(out["figures"][0])[:8] == b"\x89PNG\r\n\x1a\n"


def test_python_error_traceback(py: PythonSandbox) -> None:
    out = py.run("x = 1\ny = x / 0\n")
    assert out["error"].startswith("ZeroDivisionError")
    assert '<user_code>", line 2' in out["stderr"]


def test_python_timeout_kill(case_dir: Path) -> None:
    sb = PythonSandbox(case_dir, timeout_s=30, memory_mb=256)
    out = sb.run("print('start', flush=True)\nwhile True:\n    pass\n", timeout_s=1.5)
    assert out["error"] and "timed out" in out["error"]
    assert out["stdout"].startswith("start")
    assert out["elapsed_ms"] < 15_000


def test_python_memory_limit(py: PythonSandbox) -> None:
    out = py.run("blob = bytearray(1024 * 1024 * 1024)\nresult = len(blob)")
    assert out["error"] and "MemoryError" in out["error"]
    assert out["result_json"] is None


def test_python_network_blocked(py: PythonSandbox) -> None:
    out = py.run("import socket\ns = socket.socket()\n")
    assert out["error"] and "network access is disabled" in out["error"]
    out2 = py.run("import urllib.request\nurllib.request.urlopen('http://127.0.0.1:9', timeout=2)")
    assert out2["error"] and "network access is disabled" in out2["error"]


def test_python_output_truncated_and_cwd(py: PythonSandbox, case_dir: Path) -> None:
    out = py.run("import os\nprint('y' * 50000)\nresult = {'cwd': os.getcwd(), 'home': os.environ['HOME'], 'env': sorted(os.environ)}")
    assert "[output truncated:" in out["stdout"] and len(out["stdout"]) < OUTPUT_LIMIT_BYTES + 200
    rj = out["result_json"]
    assert rj["cwd"] == rj["home"] and str(case_dir) not in rj["cwd"]
    assert "AXIOM_DATA_DIR" not in rj["env"] and "PATH" in rj["env"]


def test_python_rejects_empty(py: PythonSandbox) -> None:
    with pytest.raises(SandboxRejected):
        py.run("   ")


# --------------------------------------------------------------------------- hardening review (bug 6)


def test_python_cannot_write_into_case_dir(case_dir: Path) -> None:
    """The case folder is read-only for user code: files, pandas exports, deletes, DuckDB writes."""
    before = sorted(p.name for p in case_dir.iterdir())
    db_bytes = (case_dir / "company.duckdb").read_bytes()
    sb = PythonSandbox(case_dir, timeout_s=30, memory_mb=256)
    attempts = [
        "open(CASE_DIR + '/evil.txt', 'w').write('x')",
        "import os\nos.open(os.path.join(CASE_DIR, 'evil.bin'), os.O_WRONLY | os.O_CREAT)",
        "tables['y'].to_csv(CASE_DIR + '/y.csv')",
        "import os\nos.remove(CASE_DIR + '/company.duckdb')",
        "import os\nos.rename(CASE_DIR + '/company.duckdb', 'stolen.duckdb')",
        "import shutil\nshutil.rmtree(CASE_DIR)",
        "open('../' * 30 + CASE_DIR.lstrip('/') + '/evil.txt', 'a').write('x')",
    ]
    for code in attempts:
        out = sb.run(code)
        assert out["error"] and out["error"].startswith("PermissionError") and "read-only" in out["error"], (code, out)
    # DuckDB: the open connection is read-only and reopening the file (even after closing `con`)
    # stays read-only; ATTACH from a new connection has no file access.
    for code in [
        "con.execute('CREATE TABLE z (i INT)')",
        "con.close()\nc2 = duckdb.connect(CASE_DIR + '/company.duckdb')\nc2.execute('CREATE TABLE z (i INT)')",
        "c2 = duckdb.connect()\nc2.execute(f\"ATTACH '{CASE_DIR}/company.duckdb' AS c\")\nc2.execute('CREATE TABLE c.z (i INT)')",
        "duckdb.execute(f\"ATTACH '{CASE_DIR}/company.duckdb' AS c2\")",
    ]:
        out = sb.run(code)
        assert out["error"], (code, out)
    # writing in the temporary working directory is fine
    ok = sb.run("open('mine.txt', 'w').write('ok')\nresult = open('mine.txt').read()")
    assert ok["error"] is None and ok["result_json"] == "ok"
    assert sorted(p.name for p in case_dir.iterdir()) == before
    assert (case_dir / "company.duckdb").read_bytes() == db_bytes


def test_python_protected_paths(tmp_path: Path, case_dir: Path) -> None:
    """Extra protected folders (the API passes the app data dir) are read-only too."""
    data = tmp_path / "appdata"
    data.mkdir()
    sb = PythonSandbox(case_dir, timeout_s=30, memory_mb=256, protected_paths=[data])
    out = sb.run(f"open({str(data / 'axiom.db')!r}, 'w').write('x')")
    assert out["error"] and "read-only" in out["error"]
    assert not (data / "axiom.db").exists()


def test_python_environment_has_no_secrets(case_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "AXIOM_LLM_API_KEY", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "sk-should-not-leak-0000")
    sb = PythonSandbox(case_dir, timeout_s=30, memory_mb=256)
    out = sb.run("import os\nresult = dict(os.environ)")
    env = out["result_json"]
    assert out["error"] is None and isinstance(env, dict)
    assert not [k for k in env if "KEY" in k or "SECRET" in k or "TOKEN" in k or k.startswith("AXIOM_")]
    assert "sk-should-not-leak" not in str(env)
    assert set(env) <= {"PATH", "HOME", "PYTHONDONTWRITEBYTECODE", "PYTHONIOENCODING", "MPLBACKEND", "MPLCONFIGDIR",
                        "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "TMPDIR", "LANG", "LC_CTYPE"}


@pytest.mark.parametrize(
    "code",
    [
        "import subprocess\nsubprocess.run(['echo', 'pwned'])",
        "import subprocess\nsubprocess.check_output('echo pwned', shell=True)",
        "import os\nos.system('echo pwned')",
        "import os\nos.popen('echo pwned').read()",
        "import os\nos.execv('/bin/echo', ['echo', 'pwned'])",
        "import os\nos.fork()",
        "import os\nos.posix_spawn('/bin/echo', ['echo', 'pwned'], {})",
        "import multiprocessing as mp\np = mp.Process(target=print)\np.start()",
        "import importlib, sys\nsys.modules.pop('subprocess', None)\nsp = importlib.import_module('subprocess')\nsp.Popen(['echo', 'pwned'])",
        "import os\nos.kill(os.getppid(), 0)",
    ],
)
def test_python_cannot_start_processes(py: PythonSandbox, code: str) -> None:
    """Decision: block (raisers for os.system/popen/exec*/spawn*/fork + an audit hook for
    subprocess.Popen and friends); ``subprocess`` stays importable because matplotlib needs it."""
    out = py.run(code)
    assert out["error"] and out["error"].startswith("PermissionError"), out
    assert "disabled in the Axiom sandbox" in out["error"]
    assert "pwned" not in out["stdout"]


def test_python_300mb_over_limit_fails_cleanly(py: PythonSandbox) -> None:
    """A 300 MB allocation with a 256 MB budget is a readable MemoryError, not a crash."""
    for code in ("blob = bytearray(300 * 1024 * 1024)", "big = np.ones(300 * 1024 * 1024 // 8)"):
        out = py.run(code + "\nresult = 'unreachable'")
        assert out["error"] and out["error"].startswith("MemoryError: the code exceeded the sandbox memory limit (256 MB)"), out
        assert out["result_json"] is None
    after = py.run("result = len(bytearray(10 * 1024 * 1024))")  # the next run is unaffected
    assert after["error"] is None and after["result_json"] == 10 * 1024 * 1024


def test_python_timeout_kills_process_group(case_dir: Path) -> None:
    """An infinite loop is killed at the timeout and its process group is gone (no orphan)."""
    import os

    sb = PythonSandbox(case_dir, timeout_s=30, memory_mb=256)
    out = sb.run("import os\nprint(os.getpid(), os.getpgid(0), flush=True)\nwhile True:\n    pass\n", timeout_s=1.5)
    assert out["error"] == "execution timed out after 1.5s and was killed"
    pid, pgid = (int(x) for x in out["stdout"].split()[:2])
    assert pid == pgid  # own session / process group
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)
