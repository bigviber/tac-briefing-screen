"""Leak-free projection of a :class:`~axiom.models.CaseWorld` for ``GET /cases/{id}``.

``CaseWorld.public_view()`` only drops ``ground_truth`` and ``dossier_seed.conceal``. The
answer key also leaks through other fields, so the API layer post-processes that dict:

* stakeholders keep only ``name``/``role``/``agenda``/``disposition`` plus the ``knows`` items
  that do not touch a concealed fact (``hides`` is dropped entirely);
* ``reveal`` items, timeline events and lever ``effect`` texts that quote a concealed string,
  an anomaly id or a 5-word fragment of the root cause are dropped / blanked;
* ``simulation.model`` is reduced to its starting ``kpis`` (``lever_effects`` encode answer
  magnitudes, ``anomaly_drag`` names the anomaly, ``resolved_if`` spells the fix);
* numeric series (statement ratios, ``operations.kpis_by_period``, the simulation KPIs) lose
  every key whose value equals an expected metric (e.g. ``ratios.ccc`` for a CCC ask);
* sample-row timestamps are normalised to the ISO 8601 format the SQL sandbox returns.

It also builds the ``asks`` (section 11) from ``ExpectedMetric`` without values or derivations.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from typing import Any

from axiom.api.schemas import Ask
from axiom.engine.sandbox import normalize_cell
from axiom.models import CaseWorld, ExpectedMetric

__all__ = [
    "ConcealScanner",
    "build_asks",
    "humanize_key",
    "key_sentence",
    "normalize_database",
    "public_case_data",
    "public_levers",
    "matches_expected_value",
]

_WORD = re.compile(r"[a-z0-9]+(?:[-'][a-z0-9]+)*")
_ID_LIKE = re.compile(r"^[A-Z]{1,3}\d{1,3}$")
_STOP = frozenset(
    "a an and are as at be been by for from has have in into is it its of on or that the their this to was "
    "were will with not no our your they them he she his her we us than then so but over under".split()
)
_SHINGLE = 5
_LOOSE_SHINGLE = 3
_LOOSE_SHARED_STEMS = 2


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower().replace("-", " "))


def _stem(word: str) -> str:
    for suffix in ("ings", "ing", "est", "ers", "er", "ed", "es", "s", "e"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _content_stems(text: str) -> set[str]:
    return {_stem(w) for w in _words(text) if w not in _STOP and len(w) > 2}


def _shingles(words: list[str], n: int) -> set[tuple[str, ...]]:
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


class ConcealScanner:
    """Detects text that discloses a concealed fact of a world.

    ``strict`` hits: a concealed phrase (case-insensitive substring), an anomaly id or short
    id-like conceal token (``A1``) as a whole word, or any 5-word shingle of the root cause.
    ``loose`` (for stakeholder ``knows``) additionally flags a 3-word shingle shared with the
    root cause or a concealed phrase, or at least two content-word stems shared with a single
    concealed phrase ("gave 90-day terms to large accounts" vs "extended payment terms for the
    largest customers").
    """

    def __init__(self, world: CaseWorld) -> None:
        conceal = [c.strip() for c in world.dossier_seed.conceal if c and c.strip()]
        self.ids = sorted({a.id for a in world.ground_truth.anomalies} | {c for c in conceal if _ID_LIKE.match(c)})
        self.phrases = [c.lower() for c in conceal if c not in self.ids and len(c) > 3]
        root = _words(world.ground_truth.root_cause)
        self.root_shingles = _shingles(root, _SHINGLE)
        self.loose_shingles = _shingles(root, _LOOSE_SHINGLE)
        for phrase in self.phrases:
            self.loose_shingles |= _shingles(_words(phrase), _LOOSE_SHINGLE)
        self.phrase_stems = [_content_stems(p) for p in self.phrases]
        self._id_re = re.compile(r"\b(" + "|".join(re.escape(i) for i in self.ids) + r")\b") if self.ids else None

    def strict(self, text: str) -> bool:
        """True when ``text`` quotes a concealed phrase, an anomaly id or a root-cause fragment."""
        if not text:
            return False
        low = " ".join(text.lower().split())
        if any(p in low for p in self.phrases):
            return True
        if self._id_re is not None and self._id_re.search(text):
            return True
        return bool(_shingles(_words(text), _SHINGLE) & self.root_shingles)

    def loose(self, text: str) -> bool:
        """``strict`` plus shared 3-word shingles or two shared content stems with one phrase."""
        if self.strict(text):
            return True
        if _shingles(_words(text), _LOOSE_SHINGLE) & self.loose_shingles:
            return True
        stems = _content_stems(text)
        return any(len(stems & ps) >= _LOOSE_SHARED_STEMS for ps in self.phrase_stems)


# --------------------------------------------------------------------------- asks

_ABBREV: dict[str, tuple[str, str]] = {  # token -> (label form, sentence form)
    "dso": ("DSO", "days sales outstanding"),
    "dio": ("DIO", "days inventory outstanding"),
    "dpo": ("DPO", "days payables outstanding"),
    "ccc": ("CCC", "cash conversion cycle"),
    "ar": ("AR", "accounts receivable"),
    "ap": ("AP", "accounts payable"),
    "wc": ("working capital", "working capital"),
    "cfo": ("CFO", "cash flow from operations"),
    "ni": ("net income", "net income"),
    "ebitda": ("EBITDA", "EBITDA"),
    "cogs": ("COGS", "cost of goods sold"),
    "sga": ("SG&A", "SG&A"),
    "arr": ("ARR", "annual recurring revenue"),
    "mrr": ("MRR", "monthly recurring revenue"),
    "nrr": ("NRR", "net revenue retention"),
    "grr": ("GRR", "gross revenue retention"),
    "ltv": ("LTV", "customer lifetime value"),
    "cac": ("CAC", "customer acquisition cost"),
    "aov": ("AOV", "average order value"),
    "kpi": ("KPI", "KPI"),
    "sku": ("SKU", "SKU"),
    "yoy": ("YoY", "year over year"),
    "qoq": ("QoQ", "quarter over quarter"),
    "p50": ("p50", "median"),
    "p95": ("p95", "95th percentile"),
    "p99": ("p99", "99th percentile"),
    "dlq": ("DLQ", "dead-letter queue"),
    "sla": ("SLA", "SLA"),
    "roi": ("ROI", "return on investment"),
    "irr": ("IRR", "internal rate of return"),
    "npv": ("NPV", "net present value"),
    "pct": ("%", "percent"),
    "delta": ("change", "change in"),
    "minus": ("minus", "minus"),
    "vs": ("vs", "versus"),
}
_UNITS = {"cents": "cents", "usd": "USD", "days": "days", "months": "months", "ratio": "ratio", "hours": "hours",
          "count": "count", "units": "units"}
_PERIOD = re.compile(r"^(\d{4})(q[1-4]|h[12]|m\d{1,2})$")
_PERIOD_IDX = re.compile(r"^p(\d{1,2})$")
_PREPOSITIONS = {"to", "in", "for", "from", "of", "vs", "over", "at", "by"}


def _tokens(key: str) -> list[str]:
    return [t for t in re.split(r"[_\-.\s]+", key.strip().lower()) if t]


def _period(token: str) -> str | None:
    m = _PERIOD.match(token)
    if m:
        return m.group(1) + m.group(2).upper()
    m = _PERIOD_IDX.match(token)
    if m and token not in _ABBREV:
        return f"period {m.group(1)}"
    return None


def humanize_key(key: str) -> str:
    """Short label from a metric key: ``dso_2024q4`` -> ``DSO 2024Q4``."""
    out: list[str] = []
    for tok in _tokens(key):
        period = _period(tok)
        if period is not None:
            out.append(period)
        elif tok in _ABBREV:
            out.append(_ABBREV[tok][0])
        elif tok in _UNITS:
            continue
        else:
            out.append(tok)
    text = " ".join(out).strip() or key
    return text[:1].upper() + text[1:]


def key_sentence(key: str) -> str:
    """A sentence from a metric key: ``dso_2024q4`` -> ``Days sales outstanding for 2024Q4``;
    consecutive periods become a range (``p6_p8`` -> ``for period 6 to period 8``) and a unit
    token becomes ``, in <unit>``."""
    words: list[str] = []
    unit = ""
    last_was_period = False
    for tok in _tokens(key):
        period = _period(tok)
        if period is not None:
            if last_was_period:
                words.append("to")
            elif not words or words[-1] not in _PREPOSITIONS:
                words.append("for")
            words.append(period)
            last_was_period = True
            continue
        if tok in _UNITS:
            unit = _UNITS[tok]
            continue
        last_was_period = False
        words.append(_ABBREV[tok][1] if tok in _ABBREV else tok)
    text = " ".join(words).strip() or key
    text = text[:1].upper() + text[1:]
    return f"{text}, in {unit}" if unit else text


def build_asks(world: CaseWorld) -> list[Ask]:
    """Section 11 asks: key, label, unit and hint per expected metric (never value/derivation)."""
    out: list[Ask] = []
    for m in world.ground_truth.expected_metrics:
        label = m.label.strip() or humanize_key(m.key)
        hint = m.hint.strip() or m.label.strip() or key_sentence(m.key)
        out.append(Ask(key=m.key, label=label, unit=m.unit, hint=hint))
    return out


# --------------------------------------------------------------------------- numeric scrub


def _significant(metric: ExpectedMetric) -> bool:
    """Values worth hiding: not booleans/flags and not tiny integers (0, 1, 3 ...)."""
    v = float(metric.value)
    if metric.unit == "bool" or not math.isfinite(v):
        return False
    return not (v.is_integer() and abs(v) < 100)


def matches_expected_value(value: Any, metrics: Iterable[ExpectedMetric]) -> bool:
    """True when ``value`` equals one of ``metrics`` (relative 1e-6), ignoring trivial values."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return False
    return any(abs(float(value) - float(m.value)) <= 1e-6 * max(1.0, abs(float(m.value))) for m in metrics)


def _scrub_series(series: dict[str, Any], metrics: list[ExpectedMetric]) -> dict[str, Any]:
    """``{period: {kpi: value}}``: drop every kpi key whose value equals an expected metric in
    any period (so the whole row disappears, not one confusing cell)."""
    leaking = {k for row in series.values() if isinstance(row, dict) for k, v in row.items()
               if matches_expected_value(v, metrics)}
    if not leaking:
        return series
    return {p: ({k: v for k, v in row.items() if k not in leaking} if isinstance(row, dict) else row)
            for p, row in series.items()}


def normalize_database(database: dict[str, Any]) -> dict[str, Any]:
    """``DatabaseSpec`` dict with sample-row cells normalised like ``/sandbox/sql`` rows."""
    for table in database.get("tables") or []:
        types = {c.get("name"): c.get("type") for c in table.get("columns") or []}
        table["sample_rows"] = [
            {k: normalize_cell(v, types.get(k)) for k, v in row.items()} for row in table.get("sample_rows") or []
        ]
    return database


# --------------------------------------------------------------------------- projection


def public_case_data(world: CaseWorld) -> dict[str, Any]:
    """The JSON-able public view of ``world`` with every known leak path removed."""
    data: dict[str, Any] = world.public_view()
    scanner = ConcealScanner(world)
    metrics = [m for m in world.ground_truth.expected_metrics if _significant(m)]

    seed = data.get("dossier_seed") or {}
    seed["stakeholders"] = [
        {
            "name": s.get("name", ""),
            "role": s.get("role", ""),
            "agenda": "" if scanner.strict(s.get("agenda", "")) else s.get("agenda", ""),
            "disposition": s.get("disposition", ""),
            "knows": [k for k in s.get("knows") or [] if not scanner.loose(k)],
        }
        for s in seed.get("stakeholders") or []
    ]
    seed["reveal"] = [r for r in seed.get("reveal") or [] if not scanner.strict(r)]
    seed["timeline"] = [e for e in seed.get("timeline") or []
                        if not (scanner.strict(e.get("headline", "")) or scanner.strict(e.get("detail", "")))]
    seed["artifacts"] = [a for a in seed.get("artifacts") or [] if not scanner.strict(a)]
    data["dossier_seed"] = seed

    sim = data.get("simulation") or {}
    kpis = (sim.get("model") or {}).get("kpis")
    sim["model"] = {"kpis": {k: v for k, v in kpis.items() if not matches_expected_value(v, metrics)}} \
        if isinstance(kpis, dict) else {}
    data["simulation"] = sim

    statements = (data.get("finance") or {}).get("statements") or {}
    if isinstance(statements.get("ratios"), dict):
        statements["ratios"] = _scrub_series(statements["ratios"], metrics)
    ops = data.get("operations") or {}
    if isinstance(ops.get("kpis_by_period"), dict):
        ops["kpis_by_period"] = _scrub_series(ops["kpis_by_period"], metrics)

    if isinstance(data.get("database"), dict):
        normalize_database(data["database"])
    return data


def public_levers(world: CaseWorld) -> list[dict[str, Any]]:
    """Decision levers (public by section 12.1) with any effect text that names the fix blanked."""
    scanner = ConcealScanner(world)
    out = []
    for lever in world.ground_truth.decision_levers:
        row = lever.model_dump(mode="json")
        if scanner.strict(row.get("effect", "")):
            row["effect"] = ""
        out.append(row)
    return out
