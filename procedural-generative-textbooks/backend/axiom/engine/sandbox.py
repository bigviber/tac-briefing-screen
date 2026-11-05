"""Execution sandboxes for learner code (ARCHITECTURE.md sections 3, 5 and 12).

* :class:`SQLSandbox` runs one read-only statement against a case's ``company.duckdb``.
  Defence in depth: a statement allowlist (single ``SELECT``/``WITH``/``EXPLAIN``/``DESCRIBE``/
  ``SHOW``/``PRAGMA table_info``), a keyword denylist, a read-only DuckDB connection with
  external (file system) access disabled and the configuration locked, and a watchdog thread
  that calls ``con.interrupt()`` when the per-query timeout elapses.
* :class:`PythonSandbox` runs learner Python in a separate interpreter (``sys.executable -I -B``)
  with address-space, CPU-time and file-size rlimits, sockets disabled, a fresh temporary
  working directory and a minimal environment. The case DuckDB is opened read-only as ``con``
  and every table is reachable as a lazily loaded ``tables["name"]`` DataFrame. A top-level
  ``result`` variable is serialized to JSON and open matplotlib figures are returned as base64
  PNGs. The child is killed (whole process group) on timeout.

The Python sandbox is a guard rail for a local, single-user tool, not a security boundary
against a hostile local user: it prevents accidents (runaway memory, infinite loops, writes to
the case DB, network calls), not determined escapes.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

__all__ = [
    "DUCKDB_READONLY_CONFIG",
    "SandboxRejected",
    "SQLSandbox",
    "PythonSandbox",
    "validate_sql",
    "json_safe",
    "truncate_text",
    "OUTPUT_LIMIT_BYTES",
]

logger = logging.getLogger("axiom.sandbox")

#: Connection config used for every read-only case connection in this process. DuckDB refuses
#: to open the same file twice in one process with different configs, so it is a constant.
DUCKDB_READONLY_CONFIG: dict[str, Any] = {
    "enable_external_access": False,
    "lock_configuration": True,
    "threads": 2,
}

OUTPUT_LIMIT_BYTES = 20_000
REFERENCE_ROW_CAP = 1000
_ALLOWED_START = re.compile(r"^(SELECT|WITH|EXPLAIN|DESCRIBE|SHOW)\b", re.IGNORECASE)
_PRAGMA_TABLE_INFO = re.compile(r"^PRAGMA\s+table_info\s*\(", re.IGNORECASE)
_WRAPPABLE = re.compile(r"^(SELECT|WITH)\b", re.IGNORECASE)
_FORBIDDEN = (
    "ATTACH", "DETACH", "COPY", "INSTALL", "LOAD", "EXPORT", "IMPORT", "CREATE", "INSERT", "UPDATE",
    "DELETE", "DROP", "ALTER", "CALL", "SET", "RESET", "CHECKPOINT", "VACUUM",
)
_FORBIDDEN_RE = re.compile(r"\b(" + "|".join(_FORBIDDEN) + r")\b", re.IGNORECASE)


class SandboxRejected(ValueError):
    """The submitted statement or code was rejected before execution (maps to HTTP 400)."""


# --------------------------------------------------------------------------- helpers


def truncate_text(text: str, limit: int = OUTPUT_LIMIT_BYTES) -> str:
    """Cap ``text`` at ``limit`` UTF-8 bytes, appending a marker that states how much was cut."""
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= limit:
        return text
    head = raw[:limit].decode("utf-8", errors="ignore")
    return head + f"\n... [output truncated: {len(raw) - limit} more bytes]"


def json_safe(value: Any) -> Any:
    """Convert a DuckDB/Python cell into a JSON-serializable value.

    ``Decimal`` -> float, date/datetime/time -> ISO string, timedelta -> seconds (float),
    bytes -> base64, NaN/inf -> ``None``, UUID -> str, containers recursively, anything else
    unknown -> ``str``.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        f = float(value)
        return f if math.isfinite(f) else None
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(v) for v in value]
    item = getattr(value, "item", None)  # numpy scalars
    if callable(item):
        try:
            return json_safe(item())
        except (TypeError, ValueError):
            pass
    return str(value)


def _strip_comments_and_literals(sql: str) -> str:
    """Remove ``--`` / ``/* */`` comments and quoted literals/identifiers (for keyword checks)."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
            out.append(" ")
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
        elif ch in ("'", '"'):
            j = i + 1
            while j < n:
                if sql[j] == ch:
                    if j + 1 < n and sql[j + 1] == ch:  # doubled quote escape
                        j += 2
                        continue
                    break
                j += 1
            i = j + 1
            out.append(" '' " if ch == "'" else ' "_" ')
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def validate_sql(sql: str) -> str:
    """Return the cleaned statement (trailing ``;`` removed) or raise :class:`SandboxRejected`."""
    if not sql or not sql.strip():
        raise SandboxRejected("empty SQL statement")
    stmt = sql.strip()
    while stmt.endswith(";"):
        stmt = stmt[:-1].rstrip()
    bare = _strip_comments_and_literals(stmt).strip()
    if ";" in bare:
        raise SandboxRejected("only a single SQL statement is allowed (remove the extra ';')")
    if not bare:
        raise SandboxRejected("empty SQL statement")
    if not (_ALLOWED_START.match(bare) or _PRAGMA_TABLE_INFO.match(bare)):
        raise SandboxRejected(
            "read-only sandbox: statements must start with SELECT, WITH, EXPLAIN, DESCRIBE, SHOW or PRAGMA table_info"
        )
    hit = _FORBIDDEN_RE.search(bare)
    if hit:
        raise SandboxRejected(f"read-only sandbox: {hit.group(1).upper()} is not allowed")
    return stmt


# --------------------------------------------------------------------------- SQL


class SQLSandbox:
    """Read-only DuckDB query runner for one case database.

    Args:
        db_path: path to ``company.duckdb``.
        timeout_s: per-query wall-clock budget; the watchdog interrupts the query after it.
    """

    def __init__(self, db_path: str | Path, *, timeout_s: float = 20.0) -> None:
        self.db_path = Path(db_path)
        self.timeout_s = float(timeout_s)

    def _connect(self) -> duckdb.DuckDBPyConnection:
        if not self.db_path.is_file():
            raise FileNotFoundError(f"case database not found: {self.db_path.name}")
        return duckdb.connect(str(self.db_path), read_only=True, config=dict(DUCKDB_READONLY_CONFIG))

    def _execute(self, sql: str, fetch: int) -> tuple[list[str], list[str], list[tuple[Any, ...]], bool]:
        """Execute with the watchdog; return ``(columns, types, rows, timed_out)``."""
        con = self._connect()
        fired = threading.Event()

        def _interrupt() -> None:
            fired.set()
            con.interrupt()

        timer = threading.Timer(self.timeout_s, _interrupt)
        timer.daemon = True
        timer.start()
        try:
            cur = con.execute(sql)
            desc = cur.description or []
            rows = cur.fetchmany(fetch) if desc else []
            return [d[0] for d in desc], [str(d[1]) for d in desc], rows, False
        except duckdb.InterruptException:
            return [], [], [], True
        except duckdb.Error:
            if fired.is_set():
                return [], [], [], True
            raise
        finally:
            timer.cancel()
            con.close()

    def run(self, sql: str, limit: int = 500) -> dict[str, Any]:
        """Run one statement. Never raises for user errors: they come back in ``error``.

        Raises:
            SandboxRejected: the statement failed the allowlist (the API maps it to 400).
        """
        stmt = validate_sql(sql)
        limit = max(1, int(limit))
        wrapped = f"SELECT * FROM (\n{stmt}\n) AS _axiom_q LIMIT {limit + 1}" if _WRAPPABLE.match(
            _strip_comments_and_literals(stmt).strip()) else stmt
        t0 = time.perf_counter()
        out: dict[str, Any] = {"columns": [], "column_types": [], "rows": [], "row_count": 0,
                               "elapsed_ms": 0.0, "truncated": False, "error": None}
        try:
            columns, types, rows, timed_out = self._execute(wrapped, limit + 1)
        except FileNotFoundError as exc:
            out["error"] = str(exc)
        except duckdb.Error as exc:
            out["error"] = f"{type(exc).__name__}: {exc}"
        else:
            if timed_out:
                out["error"] = f"query timed out after {self.timeout_s:g}s and was interrupted"
            else:
                truncated = len(rows) > limit
                rows = rows[:limit]
                out.update(
                    columns=columns,
                    column_types=types,
                    rows=[[json_safe(c) for c in r] for r in rows],
                    row_count=len(rows),
                    truncated=truncated,
                )
        out["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        if out["error"]:
            logger.info("sql sandbox error: %s", out["error"][:200])
        return out

    def run_reference(self, sql: str) -> dict[str, Any]:
        """Grader ``sql_runner``: run trusted reference SQL (capped at 1000 rows).

        Returns ``{"columns", "rows": [first_row_dict], "first_row": {...}, "row_count"}``; raises
        on failure (the grader falls back to the signature heuristic).
        """
        stmt = sql.strip().rstrip(";").strip()
        wrapped = f"SELECT * FROM (\n{stmt}\n) AS _axiom_ref LIMIT {REFERENCE_ROW_CAP}" if _WRAPPABLE.match(
            _strip_comments_and_literals(stmt).strip()) else stmt
        columns, _types, rows, timed_out = self._execute(wrapped, REFERENCE_ROW_CAP)
        if timed_out:
            raise TimeoutError(f"reference query timed out after {self.timeout_s:g}s")
        first = {c: json_safe(v) for c, v in zip(columns, rows[0])} if rows else {}
        return {"columns": columns, "rows": [first] if rows else [], "first_row": first, "row_count": len(rows)}


# --------------------------------------------------------------------------- Python

_RUNNER = r'''
import sys, os, io, json, math, base64, traceback, datetime, decimal, resource, socket, _socket

def _main(cfg_path):
    with open(cfg_path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    envelope = {"result_json": None, "figures": [], "error": None}

    def _write():
        with open(cfg["out_path"], "w", encoding="utf-8") as fh:
            json.dump(envelope, fh, allow_nan=False)

    import numpy as np
    import pandas as pd
    import duckdb
    try:
        import scipy
    except ImportError:
        scipy = None

    con = None
    names = []
    if cfg.get("db_path") and os.path.isfile(cfg["db_path"]):
        con = duckdb.connect(cfg["db_path"], read_only=True,
                             config={"enable_external_access": False, "lock_configuration": True, "threads": 1})
        names = [r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main' ORDER BY table_name").fetchall()]

    class LazyTables(dict):
        """dict of table name -> DataFrame, each loaded on first access."""

        def __init__(self, conn, table_names):
            super().__init__()
            self._con = conn
            self._names = list(table_names)

        def _load(self, key):
            if key not in self._names:
                raise KeyError(f"no table named {key!r}; available: {self._names}")
            if not dict.__contains__(self, key):
                ident = '"' + key.replace('"', '""') + '"'
                dict.__setitem__(self, key, self._con.execute(f"SELECT * FROM {ident}").df())
            return dict.__getitem__(self, key)

        def __getitem__(self, key):
            return self._load(key)

        def get(self, key, default=None):
            return self._load(key) if key in self._names else default

        def __contains__(self, key):
            return key in self._names

        def __iter__(self):
            return iter(self._names)

        def __len__(self):
            return len(self._names)

        def keys(self):
            return list(self._names)

        def items(self):
            return [(k, self._load(k)) for k in self._names]

        def values(self):
            return [self._load(k) for k in self._names]

        def __repr__(self):
            loaded = [k for k in self._names if dict.__contains__(self, k)]
            return f"<tables {self._names} (loaded: {loaded})>"

    tables = LazyTables(con, names)

    # ---- limits (after library imports so the interpreter baseline is not counted)
    def _vm_bytes():
        try:
            with open("/proc/self/status", "r", encoding="ascii") as fh:
                for line in fh:
                    if line.startswith("VmSize:"):
                        return int(line.split()[1]) * 1024
        except OSError:
            pass
        return 0

    mem = int(cfg["memory_mb"]) * 1024 * 1024
    try:
        base = _vm_bytes()
        resource.setrlimit(resource.RLIMIT_AS, (base + mem, base + mem))
    except (ValueError, OSError):
        pass
    try:
        cpu = int(cfg["cpu_s"])
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
    except (ValueError, OSError):
        pass
    try:
        fsize = 64 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
    except (ValueError, OSError):
        pass

    # ---- network off
    class _NoNetwork:
        def __init__(self, *a, **k):
            raise PermissionError("network access is disabled in the Axiom sandbox")

    socket.socket = _NoNetwork
    _socket.socket = _NoNetwork
    socket.create_connection = _NoNetwork
    socket.getaddrinfo = _NoNetwork

    with open(cfg["code_path"], "r", encoding="utf-8") as fh:
        source = fh.read()

    ns = {"__name__": "__main__", "__builtins__": __builtins__, "pd": pd, "np": np, "duckdb": duckdb,
          "con": con, "tables": tables, "CASE_DIR": cfg.get("case_dir")}
    if scipy is not None:
        ns["scipy"] = scipy
    try:
        code = compile(source, "<user_code>", "exec")
        exec(code, ns)
    except BaseException as exc:  # report every failure of user code, including SystemExit
        if isinstance(exc, SystemExit) and exc.code in (None, 0):
            pass
        else:
            tb = traceback.extract_tb(exc.__traceback__)
            user = [f for f in tb if f.filename == "<user_code>"]
            lines = traceback.format_list(user) if user else []
            sys.stderr.write("Traceback (most recent call last):\n" + "".join(lines)
                             + "".join(traceback.format_exception_only(type(exc), exc)))
            envelope["error"] = (f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__)[:2000]

    def _to_json(obj, depth=0):
        if depth > 20:
            return repr(obj)[:200]
        if obj is None or isinstance(obj, (bool, str)):
            return obj
        if isinstance(obj, int):
            return obj
        if isinstance(obj, float):
            return obj if math.isfinite(obj) else None
        if isinstance(obj, decimal.Decimal):
            f = float(obj)
            return f if math.isfinite(f) else None
        if isinstance(obj, pd.DataFrame):
            frame = obj.head(500)
            rows = [[_to_json(v, depth + 1) for v in row] for row in frame.itertuples(index=False, name=None)]
            return {"columns": [str(c) for c in obj.columns], "rows": rows,
                    "row_count": int(len(obj)), "truncated": bool(len(obj) > 500)}
        if isinstance(obj, pd.Series):
            return _to_json(obj.to_frame(name=str(obj.name) if obj.name is not None else "value").reset_index(), depth + 1)
        if obj is pd.NaT:
            return None
        if isinstance(obj, (pd.Timestamp, datetime.datetime, datetime.date, datetime.time)):
            return obj.isoformat()
        if isinstance(obj, (pd.Timedelta, datetime.timedelta)):
            return obj.total_seconds()
        if isinstance(obj, np.ndarray):
            return [_to_json(v, depth + 1) for v in obj.tolist()[:10000]]
        if isinstance(obj, np.generic):
            return _to_json(obj.item(), depth + 1)
        if isinstance(obj, dict):
            return {str(k): _to_json(v, depth + 1) for k, v in obj.items()}
        if isinstance(obj, (list, tuple, set, frozenset)):
            return [_to_json(v, depth + 1) for v in list(obj)[:10000]]
        if isinstance(obj, (bytes, bytearray)):
            return base64.b64encode(bytes(obj)).decode("ascii")
        try:
            if obj is pd.NA:
                return None
        except TypeError:
            pass
        return repr(obj)[:2000]

    if "result" in ns:
        try:
            envelope["result_json"] = _to_json(ns["result"])
            json.dumps(envelope["result_json"], allow_nan=False)
        except Exception as exc:  # serialization of arbitrary user objects
            envelope["result_json"] = None
            sys.stderr.write(f"\n[axiom] could not serialize `result`: {type(exc).__name__}: {exc}\n")

    if "matplotlib.pyplot" in sys.modules:
        try:
            plt = sys.modules["matplotlib.pyplot"]
            for num in plt.get_fignums()[:10]:
                fig = plt.figure(num)
                buf = io.BytesIO()
                fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
                envelope["figures"].append(base64.b64encode(buf.getvalue()).decode("ascii"))
            plt.close("all")
        except Exception as exc:  # figure rendering of arbitrary user figures
            sys.stderr.write(f"\n[axiom] could not render figures: {type(exc).__name__}: {exc}\n")

    sys.stdout.flush()
    sys.stderr.flush()
    _write()

if __name__ == "__main__":
    _main(sys.argv[1])
'''


class PythonSandbox:
    """Run learner Python against one case in a resource-limited subprocess.

    Args:
        case_dir: the case folder (``company.duckdb`` inside it is opened read-only).
        timeout_s: default wall-clock budget per run (the child is killed after it).
        memory_mb: address-space budget on top of the interpreter + library baseline.
        db_path: override for the DuckDB file (default ``case_dir / "company.duckdb"``).
    """

    def __init__(self, case_dir: str | Path, *, timeout_s: float = 20.0, memory_mb: int = 512,
                 db_path: str | Path | None = None) -> None:
        self.case_dir = Path(case_dir)
        self.timeout_s = float(timeout_s)
        self.memory_mb = int(memory_mb)
        self.db_path = Path(db_path) if db_path is not None else self.case_dir / "company.duckdb"

    @staticmethod
    def _env(home: Path) -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(home),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "MPLBACKEND": "Agg",
            "MPLCONFIGDIR": str(home / ".mpl"),
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "TMPDIR": str(home),
            "LANG": "C.UTF-8",
        }

    @staticmethod
    def _read_capped(path: Path) -> str:
        try:
            size = path.stat().st_size
            with path.open("rb") as fh:
                data = fh.read(OUTPUT_LIMIT_BYTES + 1)
        except OSError:
            return ""
        text = data[:OUTPUT_LIMIT_BYTES].decode("utf-8", errors="replace")
        if size > OUTPUT_LIMIT_BYTES:
            text += f"\n... [output truncated: {size - OUTPUT_LIMIT_BYTES} more bytes]"
        return text

    def run(self, code: str, *, timeout_s: float | None = None) -> dict[str, Any]:
        """Execute ``code``; returns ``{stdout, stderr, result_json, figures, elapsed_ms, error}``."""
        if not isinstance(code, str) or not code.strip():
            raise SandboxRejected("empty Python code")
        timeout = float(timeout_s if timeout_s is not None else self.timeout_s)
        control = Path(tempfile.mkdtemp(prefix="axiom_py_ctl_"))
        work = Path(tempfile.mkdtemp(prefix="axiom_py_run_"))
        out: dict[str, Any] = {"stdout": "", "stderr": "", "result_json": None, "figures": [],
                               "elapsed_ms": 0.0, "error": None}
        t0 = time.perf_counter()
        try:
            runner = control / "axiom_runner.py"
            runner.write_text(_RUNNER, encoding="utf-8")
            code_path = control / "user_code.py"
            code_path.write_text(code, encoding="utf-8")
            out_path = control / "envelope.json"
            cfg_path = control / "config.json"
            cfg_path.write_text(json.dumps({
                "db_path": str(self.db_path.resolve()) if self.db_path.is_file() else None,
                "case_dir": str(self.case_dir.resolve()),
                "code_path": str(code_path),
                "out_path": str(out_path),
                "memory_mb": self.memory_mb,
                "cpu_s": max(1, int(math.ceil(timeout)) + 1),
            }), encoding="utf-8")
            stdout_path, stderr_path = control / "stdout.txt", control / "stderr.txt"
            with stdout_path.open("wb") as so, stderr_path.open("wb") as se:
                proc = subprocess.Popen(
                    [sys.executable, "-I", "-B", str(runner), str(cfg_path)],
                    cwd=str(work), env=self._env(work), stdin=subprocess.DEVNULL, stdout=so, stderr=se,
                    start_new_session=True, close_fds=True,
                )
                timed_out = False
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    self._kill(proc)
            out["stdout"] = self._read_capped(stdout_path)
            out["stderr"] = self._read_capped(stderr_path)
            envelope: dict[str, Any] | None = None
            if out_path.is_file():
                try:
                    envelope = json.loads(out_path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    logger.warning("python sandbox envelope unreadable: %s", exc)
            if envelope is not None:
                out["result_json"] = envelope.get("result_json")
                out["figures"] = list(envelope.get("figures") or [])
                out["error"] = envelope.get("error")
            if timed_out:
                out["error"] = f"execution timed out after {timeout:g}s and was killed"
            elif envelope is None:
                rc = proc.returncode
                if rc is not None and rc < 0:
                    try:
                        sig = signal.Signals(-rc).name
                    except ValueError:
                        sig = str(-rc)
                    hint = " (CPU time limit)" if sig == "SIGXCPU" else " (memory limit?)" if sig in ("SIGKILL", "SIGSEGV") else ""
                    out["error"] = f"process killed by {sig}{hint}"
                else:
                    tail = out["stderr"].strip().splitlines()[-1:] or ["no output envelope"]
                    out["error"] = f"sandbox failed (exit {rc}): {tail[0][:500]}"
        finally:
            out["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 2)
            shutil.rmtree(control, ignore_errors=True)
            shutil.rmtree(work, ignore_errors=True)
        return out

    @staticmethod
    def _kill(proc: subprocess.Popen[bytes]) -> None:
        """Kill the child's whole process group and reap it."""
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.error("python sandbox child %s did not exit after SIGKILL", proc.pid)
