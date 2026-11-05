"""Skill DAG + Bayesian Knowledge Tracing (docs/research/pedagogy.md sections 2-3).

* :func:`bkt_update` / :func:`bkt_predict_correct` / :func:`info_gain` are the pure BKT
  equations (Corbett & Anderson 1995) with the ``[1e-4, 1 - 1e-4]`` clamp.
* :class:`SkillGraph` loads ``skill_dag.json``, validates it (acyclic, known endpoints, BKT
  degeneracy guards), syncs nodes/edges to persistence, tracks mastery, derives per-node
  observations from a :class:`~axiom.models.GradeReport` (section 2.2), applies lazy time decay
  (section 2.3), selects the next case (section 3) and summarises pillars for the dashboard.
* :class:`EloTracker` is the optional rating alternative (section 2.6).

Mastery is read through :meth:`SkillGraph.mastery`, which applies decay lazily and never
persists it. Observations update from the decayed belief.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from axiom.models import (
    ArchetypeInfo,
    Difficulty,
    GradeReport,
    MasteryRecord,
    Observation,
    Pillar,
    SkillEdge,
    SkillGraphEdge,
    SkillGraphNode,
    SkillGraphResponse,
    SkillNode,
    SkillUpdate,
)

if TYPE_CHECKING:
    from axiom.engine.rng import SeededRNG
    from axiom.models import CaseWorld
    from axiom.persistence import Persistence

__all__ = [
    "DAG_PATH",
    "PILLARS",
    "P_MIN",
    "P_MAX",
    "MASTERY_THRESHOLD",
    "UNLOCK_THRESHOLD",
    "CORRECT_THRESHOLD",
    "BORDERLINE_LOW",
    "MISDIAGNOSIS_CAP",
    "BORDERLINE_SLIP_BUMP",
    "BORDERLINE_SLIP_MAX",
    "DAGError",
    "BKTParams",
    "bkt_update",
    "bkt_predict_correct",
    "bkt_posterior",
    "binary_entropy",
    "info_gain",
    "apply_decay",
    "metric_pillar",
    "metric_anomaly_ids",
    "det_items",
    "SkillGraph",
    "EloTracker",
]

logger = logging.getLogger("axiom.skill_graph")

DAG_PATH: Path = Path(__file__).resolve().parent / "skill_dag.json"
PILLARS: tuple[Pillar, ...] = ("accounting_finance", "operations_strategy", "system_design", "sql", "python")

P_MIN = 1e-4
P_MAX = 1 - 1e-4
MASTERY_THRESHOLD = 0.95
UNLOCK_THRESHOLD = 0.60
CORRECT_THRESHOLD = 0.65
BORDERLINE_LOW = 0.50
MISDIAGNOSIS_CAP = 0.45
BORDERLINE_SLIP_BUMP = 0.15
BORDERLINE_SLIP_MAX = 0.45
DECAY_GRACE_DAYS = 21
DECAY_RATE_PER_DAY = 0.015
DECAY_MAX_SHRINK = 0.5
RECENCY_PENALTY = 0.5
RECENCY_WINDOW = 3
W_DEFICIT = 0.6
W_GAIN = 0.4

_PILLAR_ALIASES: dict[str, Pillar] = {
    "accounting_finance": "accounting_finance", "af": "accounting_finance", "acct": "accounting_finance",
    "fin": "accounting_finance", "finance": "accounting_finance",
    "operations_strategy": "operations_strategy", "ops": "operations_strategy", "os": "operations_strategy",
    "system_design": "system_design", "sd": "system_design", "sys": "system_design",
    "sql": "sql",
    "python": "python", "py": "python",
}


class DAGError(ValueError):
    """The skill DAG is malformed (cycle, unknown node, degenerate BKT parameters)."""


# --------------------------------------------------------------------------- BKT math


@dataclass(frozen=True)
class BKTParams:
    """Per-node BKT parameters."""

    p_init: float
    p_transit: float
    p_slip: float
    p_guess: float

    @classmethod
    def of(cls, node: SkillNode) -> BKTParams:
        """Parameters of a :class:`SkillNode`."""
        return cls(node.p_init, node.p_transit, node.p_slip, node.p_guess)

    def check(self, node_id: str = "") -> None:
        """Degeneracy guards (Baker, Corbett & Aleven 2008): ``G < 0.5``, ``S < 0.5``, ``S + G < 1``."""
        if not (self.p_guess < 0.5 and self.p_slip < 0.5 and self.p_slip + self.p_guess < 1):
            raise DAGError(f"degenerate BKT parameters for {node_id!r}: {self}")
        for name in ("p_init", "p_transit", "p_slip", "p_guess"):
            v = getattr(self, name)
            if not 0 <= v <= 1:
                raise DAGError(f"{node_id!r}: {name}={v} outside [0, 1]")


def _clamp(p: float) -> float:
    return min(P_MAX, max(P_MIN, p))


def bkt_posterior(p: float, correct: bool, prm: BKTParams, *, slip: float | None = None) -> float:
    """Step 1 of the BKT update: ``P(known | observation)``."""
    s = prm.p_slip if slip is None else slip
    g = prm.p_guess
    if correct:
        num, den = p * (1 - s), p * (1 - s) + (1 - p) * g
    else:
        num, den = p * s, p * s + (1 - p) * (1 - g)
    return num / den if den > 0 else p


def bkt_update(p: float, correct: bool, prm: BKTParams, *, slip_adjust: float = 0.0) -> float:
    """Full BKT update (posterior then learning transition), clamped to ``[1e-4, 1-1e-4]``.

    ``slip_adjust > 0`` raises the slip for this one update to ``min(0.45, p_slip + slip_adjust)``
    (the borderline band of section 2.2).
    """
    slip = min(BORDERLINE_SLIP_MAX, prm.p_slip + slip_adjust) if slip_adjust else prm.p_slip
    post = bkt_posterior(p, correct, prm, slip=slip)
    return _clamp(post + (1 - post) * prm.p_transit)


def bkt_predict_correct(p: float, prm: BKTParams) -> float:
    """``P(next observation correct)``."""
    return p * (1 - prm.p_slip) + (1 - p) * prm.p_guess


def binary_entropy(p: float) -> float:
    """Binary entropy in bits."""
    if p <= 0 or p >= 1:
        return 0.0
    return -(p * math.log2(p) + (1 - p) * math.log2(1 - p))


def info_gain(p: float, prm: BKTParams) -> float:
    """Expected entropy reduction of one observation: ``H(p) - (c H(p_c) + (1-c) H(p_i))``."""
    c = bkt_predict_correct(p, prm)
    p_c = bkt_posterior(p, True, prm)
    p_i = bkt_posterior(p, False, prm)
    return binary_entropy(p) - (c * binary_entropy(p_c) + (1 - c) * binary_entropy(p_i))


def apply_decay(p: float, p_init: float, last_observed: dt.datetime | None, now: dt.datetime) -> float:
    """Lazy decay toward the prior (section 2.3).

    After 21 idle days, ``p = p_init + (p - p_init) * max(0.5, 1 - 0.015 * (days_idle - 21))``.
    """
    if last_observed is None:
        return p
    days_idle = (now - last_observed).total_seconds() / 86400.0
    if days_idle <= DECAY_GRACE_DAYS:
        return p
    factor = max(1 - DECAY_MAX_SHRINK, 1 - DECAY_RATE_PER_DAY * (days_idle - DECAY_GRACE_DAYS))
    return _clamp(p_init + (p - p_init) * factor)


def _parse_ts(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        ts = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=dt.UTC)


# --------------------------------------------------------------------------- grade -> items


def metric_pillar(key: str, world: CaseWorld | None = None) -> Pillar | None:
    """Pillar tag of an expected metric.

    0. An explicit ``ExpectedMetric.pillar`` wins.
    1. Key prefix: ``"<pillar>."``, ``"<pillar>:"``, ``"<pillar>/"`` or ``"<pillar>__"`` where
       ``<pillar>`` is a pillar id or alias (``af``, ``ops``, ``sd``, ``py`` ...), or a full pillar
       id followed by ``_``.
    2. Otherwise the pillar of the first anomaly whose ``magnitude`` has that key, or whose
       reference query ``expected["metric_keys"]`` lists it; with exactly one anomaly in the
       world, that anomaly's pillar.
    3. Otherwise ``None`` (untagged; still counts toward ``det_all``).
    """
    if world is not None:
        for em in world.ground_truth.expected_metrics:
            explicit = getattr(em, "pillar", None)
            if em.key == key and explicit:
                return explicit  # type: ignore[no-any-return]
    low = key.lower()
    for sep in (".", ":", "/", "__"):
        if sep in low:
            head = low.split(sep, 1)[0]
            if head in _PILLAR_ALIASES:
                return _PILLAR_ALIASES[head]
    for pillar in PILLARS:
        if low.startswith(pillar + "_"):
            return pillar
    if world is not None:
        anomalies = world.ground_truth.anomalies
        for anomaly in anomalies:
            if key in metric_keys_of_anomaly(anomaly):
                return anomaly.pillar
        if len(anomalies) == 1:
            return anomalies[0].pillar
    return None


def metric_keys_of_anomaly(anomaly: Any) -> set[str]:
    """Metric keys an anomaly declares: its ``magnitude`` keys plus any reference query's
    ``expected["metric_keys"]``."""
    keys = set(anomaly.magnitude)
    for q in anomaly.reference_sql:
        mk = q.expected.get("metric_keys") if isinstance(q.expected, Mapping) else None
        if isinstance(mk, list):
            keys.update(str(k) for k in mk)
    return keys


def metric_anomaly_ids(key: str, world: CaseWorld) -> list[str]:
    """Anomaly ids linked to a metric key (magnitude / ``metric_keys`` / ``"<id>."`` prefix). When
    the world has exactly one anomaly, every metric is linked to it."""
    anomalies = world.ground_truth.anomalies
    if len(anomalies) == 1:
        return [anomalies[0].id]
    out = []
    for a in anomalies:
        if key in metric_keys_of_anomaly(a) or key.lower().startswith(a.id.lower() + "."):
            out.append(a.id)
    return out


def det_items(grade: GradeReport) -> list[tuple[Pillar | None, bool]]:
    """All evaluated deterministic items as ``(pillar_tag, passed)``."""
    items: list[tuple[Pillar | None, bool]] = [(m.pillar, m.within_tolerance) for m in grade.deterministic.metric_results]
    items.extend((q.pillar, q.matched) for q in grade.deterministic.query_signature_matches if q.evaluated)
    return items


def _dim(grade: GradeReport, name: str) -> float:
    d = grade.rubric.dimensions.get(name)
    return float(d.score) / 10.0 if d is not None else 0.0


def _weighted(terms: Sequence[tuple[float, float | None]]) -> float:
    """Weighted sum, renormalising over terms whose value is ``None``."""
    present = [(w, v) for w, v in terms if v is not None]
    total_w = sum(w for w, _ in present)
    if total_w <= 0:
        return 0.0
    return sum(w * v for w, v in present) / total_w


# --------------------------------------------------------------------------- SkillGraph


class SkillGraph:
    """Skill DAG with BKT mastery tracking, observation derivation and case selection."""

    def __init__(
        self,
        nodes: Iterable[SkillNode],
        edges: Iterable[SkillEdge],
        archetype_map: Mapping[str, Sequence[str]] | None = None,
        *,
        meta: Mapping[str, Any] | None = None,
        pillars: Sequence[Mapping[str, Any]] | None = None,
        persistence: Persistence | None = None,
        clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        """Build a graph from parsed nodes/edges. Prefer :meth:`load`."""
        self.nodes: dict[str, SkillNode] = {n.id: n for n in nodes}
        edge_set = {(e.src, e.dst) for e in edges}
        for n in self.nodes.values():  # node-declared prereqs are edges too
            edge_set.update((p, n.id) for p in n.prereqs)
        self.edges: list[SkillEdge] = [SkillEdge(src=s, dst=d) for s, d in sorted(edge_set)]
        self.archetype_map: dict[str, list[str]] = {k: list(v) for k, v in (archetype_map or {}).items()}
        self.meta: dict[str, Any] = dict(meta or {})
        self.pillars_meta: list[dict[str, Any]] = [dict(p) for p in (pillars or [])]
        self.persistence = persistence
        self._clock = clock or (lambda: dt.datetime.now(dt.UTC))
        self.mastery_threshold = float(self.meta.get("mastery_threshold", MASTERY_THRESHOLD))
        self.unlock_threshold = float(self.meta.get("prereq_unlock_threshold", UNLOCK_THRESHOLD))
        self._prereqs: dict[str, list[str]] = {nid: [] for nid in self.nodes}
        for e in self.edges:
            if e.src not in self.nodes or e.dst not in self.nodes:
                raise DAGError(f"edge {e.src} -> {e.dst} references an unknown node")
            self._prereqs[e.dst].append(e.src)
        self.topological_order: list[str] = self._toposort()
        for nid, node in self.nodes.items():
            BKTParams.of(node).check(nid)
        for arch, ids in self.archetype_map.items():
            unknown = [i for i in ids if i not in self.nodes]
            if unknown:
                raise DAGError(f"archetype {arch!r} references unknown nodes {unknown}")
        self._records: dict[str, MasteryRecord] = {}
        self.history: dict[str, list[dict[str, Any]]] = {nid: [] for nid in self.nodes}
        if persistence is not None:
            self._records = {k: v for k, v in persistence.get_mastery_all().items() if k in self.nodes}

    # ------------------------------------------------------------------ loading

    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        persistence: Persistence | None = None,
        *,
        clock: Callable[[], dt.datetime] | None = None,
    ) -> SkillGraph:
        """Load ``skill_dag.json`` (default: the packaged one), validate it, and when a
        persistence handle is given ensure the schema, sync nodes/edges and load mastery."""
        dag_path = Path(path) if path is not None else DAG_PATH
        raw = json.loads(dag_path.read_text(encoding="utf-8"))
        meta = raw.get("meta", {})
        presets = meta.get("bkt_presets", {})
        nodes: list[SkillNode] = []
        for record in raw.get("nodes", []):
            rec = dict(record)
            if not rec.get("bkt"):
                rec["bkt"] = presets.get(rec.get("tier", "core"), {})
            nodes.append(SkillNode.from_dag_record(rec))
        edges = []
        for e in raw.get("edges", []):
            if isinstance(e, Mapping):
                edges.append(SkillEdge(src=e["src"], dst=e["dst"]))
            else:
                edges.append(SkillEdge(src=e[0], dst=e[1]))
        graph = cls(nodes, edges, raw.get("archetype_nodes", {}), meta=meta, pillars=raw.get("pillars"),
                    persistence=None, clock=clock)
        if persistence is not None:
            persistence.init_schema()
            persistence.upsert_skill_nodes(graph.nodes.values())
            persistence.replace_skill_edges(graph.edges)
            graph.persistence = persistence
            graph._records = {k: v for k, v in persistence.get_mastery_all().items() if k in graph.nodes}
        logger.info("skill DAG loaded: %d nodes, %d edges, %d archetypes", len(graph.nodes), len(graph.edges), len(graph.archetype_map))
        return graph

    def _toposort(self) -> list[str]:
        indeg = {nid: len(set(ps)) for nid, ps in self._prereqs.items()}
        children: dict[str, list[str]] = {nid: [] for nid in self.nodes}
        for e in self.edges:
            children[e.src].append(e.dst)
        ready = sorted(n for n, d in indeg.items() if d == 0)
        order: list[str] = []
        while ready:
            n = ready.pop(0)
            order.append(n)
            for c in sorted(children[n]):
                indeg[c] -= 1
                if indeg[c] == 0:
                    ready.append(c)
            ready.sort()
        if len(order) != len(self.nodes):
            cyclic = sorted(set(self.nodes) - set(order))
            raise DAGError(f"skill DAG has a cycle among {cyclic[:10]}")
        return order

    # ------------------------------------------------------------------ structure

    def prereqs(self, node: str) -> list[str]:
        """Direct prerequisites of ``node``."""
        return sorted(set(self._prereqs[node]))

    def pillar_of(self, node: str) -> Pillar:
        """Pillar of ``node``."""
        return self.nodes[node].pillar

    def archetype_nodes(self, archetype: str) -> list[str]:
        """Nodes an archetype exercises (``archetype_nodes`` in the DAG; single source of truth)."""
        return list(self.archetype_map.get(archetype, []))

    def params(self, node: str) -> BKTParams:
        """BKT parameters of ``node``."""
        return BKTParams.of(self.nodes[node])

    # ------------------------------------------------------------------ mastery

    def _now(self) -> dt.datetime:
        now = self._clock()
        return now if now.tzinfo else now.replace(tzinfo=dt.UTC)

    def record(self, node_id: str) -> MasteryRecord:
        """Stored mastery row (undecayed), or a fresh prior row if never observed."""
        rec = self._records.get(node_id)
        if rec is None:
            return MasteryRecord(node_id=node_id, p_known=self.nodes[node_id].p_init)
        return rec

    def mastery(self, node_id: str, *, decayed: bool = True) -> float:
        """``P(known)`` for a node, with lazy decay applied unless ``decayed=False``."""
        if node_id not in self.nodes:
            raise KeyError(node_id)
        rec = self._records.get(node_id)
        if rec is None:
            return self.nodes[node_id].p_init
        if not decayed:
            return rec.p_known
        return apply_decay(rec.p_known, self.nodes[node_id].p_init, _parse_ts(rec.last_seen), self._now())

    def mastery_all(self, *, decayed: bool = True) -> dict[str, float]:
        """Mastery for every node."""
        return {nid: self.mastery(nid, decayed=decayed) for nid in self.nodes}

    def is_decayed(self, node_id: str) -> bool:
        """True when the node is idle > 21 days (UI: hollow ring, "due")."""
        rec = self._records.get(node_id)
        ts = _parse_ts(rec.last_seen) if rec else None
        return ts is not None and (self._now() - ts).total_seconds() / 86400.0 > DECAY_GRACE_DAYS

    def decay(self, today: dt.datetime | None = None) -> dict[str, float]:
        """Decayed mastery view at ``today`` (default: now). Decay is lazy and never persisted
        (section 2.3), so this returns the view and leaves stored rows untouched."""
        now = today or self._now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=dt.UTC)
        out: dict[str, float] = {}
        for nid, node in self.nodes.items():
            rec = self._records.get(nid)
            out[nid] = node.p_init if rec is None else apply_decay(rec.p_known, node.p_init, _parse_ts(rec.last_seen), now)
        return out

    def is_mastered(self, node_id: str) -> bool:
        """``p >= 0.95``."""
        return self.mastery(node_id) >= self.mastery_threshold

    def eligible_nodes(self) -> set[str]:
        """Nodes whose every prerequisite has ``p >= prereq_unlock_threshold`` (0.60)."""
        m = self.mastery_all()
        return {nid for nid in self.nodes if all(m[p] >= self.unlock_threshold for p in self._prereqs[nid])}

    def update(
        self,
        node_id: str,
        correct: bool,
        *,
        slip_adjust: float = 0.0,
        case_id: str | None = None,
        score: float | None = None,
        outcome: str | None = None,
    ) -> SkillUpdate:
        """Apply one BKT observation (from the decayed belief) and persist the row."""
        prm = self.params(node_id)
        before = self.mastery(node_id)
        after = bkt_update(before, correct, prm, slip_adjust=slip_adjust)
        old = self.record(node_id)
        now_iso = self._now().replace(microsecond=0).isoformat()
        rec = MasteryRecord(
            node_id=node_id, p_known=after, attempts=old.attempts + 1,
            correct=old.correct + (1 if correct else 0), last_seen=now_iso,
        )
        self._records[node_id] = rec
        if self.persistence is not None:
            self.persistence.upsert_mastery(rec)
        self.history[node_id].append(
            {"case_id": case_id, "s_k": score, "obs": outcome or ("correct" if correct else "incorrect"),
             "p_before": before, "p_after": after, "at": now_iso}
        )
        logger.debug("bkt %s %s: %.4f -> %.4f", node_id, outcome or correct, before, after)
        return SkillUpdate(node=node_id, before=round(before, 6), after=round(after, 6), score=score,
                           outcome=outcome or ("correct" if correct else "incorrect"))  # type: ignore[arg-type]

    def set_mastery(self, node_id: str, p: float, *, last_seen: str | None = None) -> None:
        """Overwrite a node's stored ``p_known`` (admin/testing; not an observation)."""
        if node_id not in self.nodes:
            raise KeyError(node_id)
        old = self.record(node_id)
        rec = MasteryRecord(node_id=node_id, p_known=_clamp(p), attempts=old.attempts, correct=old.correct,
                            last_seen=last_seen or self._now().replace(microsecond=0).isoformat())
        self._records[node_id] = rec
        if self.persistence is not None:
            self.persistence.upsert_mastery(rec)

    def reset(self) -> None:
        """Forget all mastery (persisted rows too)."""
        self._records.clear()
        self.history = {nid: [] for nid in self.nodes}
        if self.persistence is not None:
            self.persistence.reset_mastery()

    # ------------------------------------------------------------------ grade -> observations

    def case_nodes(self, world: CaseWorld) -> list[str]:
        """Nodes a case updates: ``world.skill_nodes`` (populated from ``archetype_nodes`` at
        generation), falling back to the DAG's ``archetype_nodes``; unknown ids are dropped."""
        ids = list(world.skill_nodes) or self.archetype_nodes(world.archetype)
        return [n for n in dict.fromkeys(ids) if n in self.nodes]

    def node_scores_from_grade(self, grade_report: GradeReport, world: CaseWorld) -> dict[str, float]:
        """Node score ``s_k`` per case node (section 2.2 table), misdiagnosis cap applied.

        ``det_all`` is the pass fraction over all evaluated deterministic items; ``det_k`` is the
        pass fraction over items tagged with the node's pillar. When there are no deterministic
        items at all the ``det_all`` term is dropped and the remaining weights renormalised.
        """
        items = det_items(grade_report)
        det_all = (sum(1 for _, ok in items if ok) / len(items)) if items else None
        te = _dim(grade_report, "technical_execution")
        bj = _dim(grade_report, "business_judgment")
        risk = _dim(grade_report, "risk_awareness")
        scores: dict[str, float] = {}
        for nid in self.case_nodes(world):
            pillar = self.pillar_of(nid)
            if pillar in ("sql", "python"):
                tagged = [ok for p, ok in items if p == pillar]
                if tagged:
                    s = 0.6 * (sum(tagged) / len(tagged)) + 0.4 * te
                else:
                    s = te
            elif pillar == "accounting_finance":
                s = _weighted([(0.5, det_all), (0.3, bj), (0.2, te)])
            elif pillar == "operations_strategy":
                s = _weighted([(0.4, det_all), (0.4, bj), (0.2, risk)])
            else:  # system_design
                s = _weighted([(0.3, det_all), (0.3, te), (0.4, risk)])
            if grade_report.rubric.misdiagnosis:
                s = min(s, MISDIAGNOSIS_CAP)
            scores[nid] = round(s, 6)
        return scores

    @staticmethod
    def observation_for_score(node: str, pillar: Pillar, score: float) -> Observation:
        """Threshold a node score: ``>= 0.65`` correct; ``[0.50, 0.65)`` borderline (miss with
        slip raised by 0.15); below 0.50 incorrect."""
        if score >= CORRECT_THRESHOLD:
            return Observation(node=node, pillar=pillar, score=score, correct=True, outcome="correct")
        if score >= BORDERLINE_LOW:
            return Observation(node=node, pillar=pillar, score=score, correct=False, outcome="borderline",
                               slip_adjust=BORDERLINE_SLIP_BUMP)
        return Observation(node=node, pillar=pillar, score=score, correct=False, outcome="incorrect")

    def observations_from_grade(self, grade_report: GradeReport, world: CaseWorld) -> list[Observation]:
        """Exactly one observation per case node (section 2.2)."""
        scores = self.node_scores_from_grade(grade_report, world)
        return [self.observation_for_score(nid, self.pillar_of(nid), s) for nid, s in scores.items()]

    def apply_grade(self, grade_report: GradeReport, world: CaseWorld) -> list[SkillUpdate]:
        """Apply the grade's observations. Only the first graded submission of a case counts;
        the caller (``Pedagogy.grade``) enforces that."""
        updates = []
        for obs in self.observations_from_grade(grade_report, world):
            updates.append(
                self.update(obs.node, obs.correct, slip_adjust=obs.slip_adjust, case_id=world.case_id,
                            score=obs.score, outcome=obs.outcome)
            )
        return updates

    # ------------------------------------------------------------------ selection

    def select_case(
        self,
        archetype_catalog: Sequence[ArchetypeInfo],
        rng: SeededRNG,
        *,
        difficulty: Difficulty | None = None,
        target_skills: Sequence[str] | None = None,
        recent_cases: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Pick the next case (section 3).

        Score per archetype over its *usable* nodes (eligible, or explicitly targeted):
        ``0.6 * sum(1 - p) + 0.4 * sum(info_gain) - 0.5 * [archetype in last 3 recent]``.
        Archetypes with fewer than two usable nodes are skipped; if that leaves nothing (cold
        start) archetypes with one usable node are scored, and as a last resort prerequisites are
        ignored. Ties (|delta| < 1e-6) are broken by
        ``rng`` so the same mastery state + seed proposes the same case. Difficulty comes from
        the mean mastery of the usable nodes unless given, and is clamped into the archetype's
        ``difficulty_range``. ``target_skills`` restricts candidates to archetypes covering at
        least one target (ignored with a note if none does).

        Returns ``{archetype, difficulty, skill_nodes, rationale, score, candidates}``.
        """
        mastery = self.mastery_all()
        eligible = self.eligible_nodes()
        targets = [t for t in (target_skills or []) if t in self.nodes]
        notes: list[str] = []
        catalog = list(archetype_catalog)
        if not catalog:
            raise ValueError("archetype catalog is empty")

        def nodes_of(info: ArchetypeInfo) -> list[str]:
            ids = list(info.skill_nodes) or self.archetype_nodes(info.key)
            return [n for n in dict.fromkeys(ids) if n in self.nodes]

        if targets:
            covering = [a for a in catalog if set(nodes_of(a)) & set(targets)]
            if covering:
                catalog = covering
                notes.append(f"restricted to archetypes covering targets {targets}")
            else:
                notes.append(f"no archetype covers targets {targets}; ignoring targets")
                targets = []
        recent = list(recent_cases)[-RECENCY_WINDOW:]

        def score_all(require_eligible: bool, min_usable: int) -> list[tuple[float, str, list[str], float, float, float]]:
            out = []
            for info in catalog:
                nodes = nodes_of(info)
                usable = [k for k in nodes if (k in eligible or k in targets)] if require_eligible else nodes
                if len(usable) < min_usable:
                    continue
                gain = sum(info_gain(mastery[k], self.params(k)) for k in usable)
                deficit = sum(1 - mastery[k] for k in usable)
                penalty = RECENCY_PENALTY if info.key in recent else 0.0
                out.append((W_DEFICIT * deficit + W_GAIN * gain - penalty, info.key, usable, deficit, gain, penalty))
            return out

        scored = score_all(True, 2)
        if not scored:
            # Cold start: from the priors only the prerequisite-free nodes are eligible and no
            # archetype has two of them. Prefer archetypes with at least one eligible node.
            notes.append("no archetype has two eligible nodes; scoring on single eligible nodes")
            scored = score_all(True, 1)
        if not scored:
            notes.append("no archetype has an eligible node; prerequisites ignored")
            scored = score_all(False, 2)
        if not scored:
            raise ValueError("no archetype in the catalog maps to at least two known skill nodes")
        scored.sort(key=lambda t: (-round(t[0], 6), t[1]))
        best = scored[0][0]
        top = [t for t in scored if abs(t[0] - best) < 1e-6]
        pick = top[int(rng.integers(0, len(top)))] if len(top) > 1 else top[0]
        score, key, usable, deficit, gain, penalty = pick
        info = next(a for a in catalog if a.key == key)

        m = sum(mastery[k] for k in usable) / len(usable)
        computed: Difficulty = "foundation" if m < 0.35 else "practitioner" if m < 0.75 else "principal"
        chosen: Difficulty = difficulty or computed
        order: list[Difficulty] = ["foundation", "practitioner", "principal"]
        if info.difficulty_range and chosen not in info.difficulty_range:
            allowed = [d for d in order if d in info.difficulty_range]
            chosen = min(allowed, key=lambda d: abs(order.index(d) - order.index(chosen)))
            notes.append(f"difficulty clamped into {info.difficulty_range}")

        weakest = sorted(usable, key=lambda k: mastery[k])[:3]
        rationale = (
            f"{info.title or key}: targets {len(usable)} nodes with mean mastery {m:.2f} "
            f"(deficit {deficit:.2f}, info gain {gain:.2f} bits"
            + (f", recency penalty {penalty:.1f}" if penalty else "")
            + f"); weakest: {', '.join(f'{k} ({mastery[k]:.2f})' for k in weakest)}."
            + (f" {len(top)}-way tie broken by seed." if len(top) > 1 else "")
            + (" " + "; ".join(notes) if notes else "")
        )
        return {
            "archetype": key,
            "difficulty": chosen,
            "skill_nodes": usable,
            "rationale": rationale,
            "score": round(score, 6),
            "candidates": [{"archetype": t[1], "score": round(t[0], 6)} for t in scored],
        }

    # ------------------------------------------------------------------ views

    def pillar_summary(self) -> dict[str, dict[str, Any]]:
        """Per-pillar ``{mean_mastery, nodes, mastered}`` for the dashboard."""
        m = self.mastery_all()
        out: dict[str, dict[str, Any]] = {}
        for pillar in PILLARS:
            ids = [nid for nid, n in self.nodes.items() if n.pillar == pillar]
            if not ids:
                continue
            out[pillar] = {
                "mean_mastery": round(sum(m[i] for i in ids) / len(ids), 6),
                "nodes": len(ids),
                "mastered": sum(1 for i in ids if m[i] >= self.mastery_threshold),
            }
        return out

    def graph_response(self) -> SkillGraphResponse:
        """``GET /skills/graph`` payload."""
        m = self.mastery_all()
        nodes = [
            SkillGraphNode(id=nid, pillar=n.pillar, title=n.title, mastery=round(m[nid], 6),
                           prereqs=self.prereqs(nid), attempts=self.record(nid).attempts)
            for nid, n in sorted(self.nodes.items())
        ]
        edges = [SkillGraphEdge(src=e.src, dst=e.dst) for e in self.edges]
        return SkillGraphResponse(nodes=nodes, edges=edges)


# --------------------------------------------------------------------------- ELO alternative


class EloTracker:
    """Optional ELO mastery model (section 2.6); not the default.

    Node ratings start at 1200; case difficulty rating is ``{foundation: 1100, practitioner:
    1300, principal: 1500}`` plus 50 per additional simultaneous anomaly. ``K = 40`` for the first
    five observations of a node, then 20. Display mastery ``1 / (1 + 10^((1500 - R)/400))``.
    """

    START = 1200.0
    CASE_RATING: dict[str, float] = {"foundation": 1100.0, "practitioner": 1300.0, "principal": 1500.0}

    def __init__(self, ratings: Mapping[str, float] | None = None, counts: Mapping[str, int] | None = None) -> None:
        self.ratings: dict[str, float] = dict(ratings or {})
        self.counts: dict[str, int] = dict(counts or {})

    @classmethod
    def case_rating(cls, difficulty: str, n_anomalies: int = 1) -> float:
        """Difficulty rating ``D`` of a case."""
        return cls.CASE_RATING.get(difficulty, 1300.0) + 50.0 * max(0, n_anomalies - 1)

    @staticmethod
    def expected(rating: float, case_rating: float) -> float:
        """``E = 1 / (1 + 10^((D - R)/400))``."""
        return 1.0 / (1.0 + 10 ** ((case_rating - rating) / 400.0))

    def rating(self, node_id: str) -> float:
        """Current rating (1200 if unseen)."""
        return self.ratings.get(node_id, self.START)

    def mastery(self, node_id: str) -> float:
        """Display probability ``1 / (1 + 10^((1500 - R)/400))``."""
        return 1.0 / (1.0 + 10 ** ((1500.0 - self.rating(node_id)) / 400.0))

    def observe(self, node_id: str, score: float, *, difficulty: str = "practitioner", n_anomalies: int = 1) -> SkillUpdate:
        """Update with a continuous node score ``S = s_k`` in ``[0, 1]``."""
        r = self.rating(node_id)
        d = self.case_rating(difficulty, n_anomalies)
        k = 40.0 if self.counts.get(node_id, 0) < 5 else 20.0
        before = self.mastery(node_id)
        self.ratings[node_id] = r + k * (min(1.0, max(0.0, score)) - self.expected(r, d))
        self.counts[node_id] = self.counts.get(node_id, 0) + 1
        return SkillUpdate(node=node_id, before=round(before, 6), after=round(self.mastery(node_id), 6), score=score)

    def apply_grade(self, grade_report: GradeReport, world: CaseWorld, graph: SkillGraph) -> list[SkillUpdate]:
        """Observe every case node's continuous score from ``graph.node_scores_from_grade``."""
        scores = graph.node_scores_from_grade(grade_report, world)
        n_anom = max(1, len(world.ground_truth.anomalies))
        return [self.observe(nid, s, difficulty=world.difficulty, n_anomalies=n_anom) for nid, s in scores.items()]
