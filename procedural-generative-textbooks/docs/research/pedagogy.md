# Axiom Pedagogy Specification

This document is the implementation spec for `axiom/skill_graph.py`, `axiom/skill_dag.json`
and the prompt templates in `engine/pedagogy.py`. It is written so an engineer can implement
each piece without further research. Where it makes a choice, it says why, and cites the
learning-science literature the choice rests on (§9).

Companion files:

- `backend/axiom/skill_dag.json` — 65 nodes, 5 pillars, 73 prerequisite edges, per-node BKT
  priors, archetype→node mapping (`archetype_nodes`).
- `backend/axiom/engine/pedagogy_fixtures/*.json` — three Submission fixtures with expected
  rubric ranges for offline tests.

---

## 1. Design stance

Axiom is a case-method system, not a quiz engine. The unit of learning is a generated company
with a hidden root cause; the unit of assessment is an executive deliverable backed by code.
That changes three things relative to a classic intelligent tutoring system:

1. **Observations are rich, not binary.** A submission produces a deterministic metric check
   and a four-dimension rubric. We reduce these to per-node binary observations for Bayesian
   Knowledge Tracing (BKT) using explicit thresholds (§2.2), but we keep the rubric itself as
   the thing the learner sees.
2. **One case exercises several skills across at least two pillars.** Every archetype maps to
   4–5 nodes spanning ≥2 pillars (`archetype_nodes` in the DAG). A single grade therefore updates
   several nodes, each with its own observation (§2.2).
3. **The learner is supposed to struggle first.** The dossier conceals the root cause; the
   learner attempts a diagnosis before the textbook chapter is unlocked for the nodes they
   missed. This is the productive-failure sequence (Kapur 2008): attempt, then consolidate.

---

## 2. Mastery model: Bayesian Knowledge Tracing

### 2.1 Equations

For each node *k* the learner has a latent binary state *L* ∈ {known, unknown} and we track
`p = P(L = known)`. Four parameters per node, read from `skill_dag.json`:

| Symbol | JSON key | Meaning |
|---|---|---|
| P(L₀) | `p_init` | prior that the skill is known before any observation |
| P(T) | `p_transit` | probability of learning the skill on each opportunity |
| P(S) | `p_slip` | probability of an incorrect observation when the skill is known |
| P(G) | `p_guess` | probability of a correct observation when the skill is unknown |

Given an observation `obs ∈ {correct, incorrect}` the update is the standard two-step
(Corbett & Anderson 1995):

**Step 1 — posterior given evidence.**

```
if obs == correct:
    p_post = p * (1 - S) / ( p * (1 - S) + (1 - p) * G )
else:
    p_post = p * S / ( p * S + (1 - p) * (1 - G) )
```

**Step 2 — learning transition.**

```
p_next = p_post + (1 - p_post) * T
```

Implement as pure functions:

```python
def bkt_update(p: float, correct: bool, prm: BKTParams) -> float:
    s, g, t = prm.p_slip, prm.p_guess, prm.p_transit
    if correct:
        post = p * (1 - s) / (p * (1 - s) + (1 - p) * g)
    else:
        post = p * s / (p * s + (1 - p) * (1 - g))
    return post + (1 - post) * t

def bkt_predict_correct(p: float, prm: BKTParams) -> float:
    """P(next observation correct) — used for case selection (§3)."""
    return p * (1 - prm.p_slip) + (1 - p) * prm.p_guess
```

Clamp `p` to `[1e-4, 1 - 1e-4]` after every update to avoid saturation making later evidence
inert.

### 2.2 Mapping a GradeReport to per-node observations

A `GradeReport` (ARCHITECTURE §5) carries:

- `deterministic.metrics[]` — each expected metric with `within_tolerance: bool`;
- `deterministic.queries[]` — each reference-query signature with `matched: bool`;
- `rubric.dimensions` — `technical_execution`, `business_judgment`, `communication`,
  `risk_awareness`, each 0–10 with evidence quotes;
- `rubric.misdiagnosis: bool`;
- `partial_diagnosis: bool` — the grader's "right area, wrong mechanism" flag. `RubricGrade`
  has no field for it (and ignores unknown fields), so `Pedagogy.grade` carries it as
  `rubric.raw_scores["partial_diagnosis"] == 1.0` plus a `rubric.clamps` note; read it with
  `pedagogy.is_partial_diagnosis(rubric)`. A partial diagnosis always also sets `misdiagnosis`.

Each node in the case's `archetype_nodes` list receives **exactly one observation per
submission**. The observation is derived from a *node score* `s_k ∈ [0,1]` built from the
evidence that is relevant to that node's pillar:

| Node pillar | Node score `s_k` |
|---|---|
| `sql`, `python` | `0.6 · det_k + 0.4 · (technical_execution / 10)` where `det_k` is the fraction of expected metrics and reference queries tagged with that node's pillar that passed (tags come from `Anomaly.pillar` and `ExpectedMetric.key` prefix). If no deterministic items are tagged for the pillar, `s_k = technical_execution / 10`. |
| `accounting_finance` | `0.5 · det_all + 0.3 · (business_judgment / 10) + 0.2 · (technical_execution / 10)` |
| `operations_strategy` | `0.4 · det_all + 0.4 · (business_judgment / 10) + 0.2 · (risk_awareness / 10)` |
| `system_design` | `0.3 · det_all + 0.3 · (technical_execution / 10) + 0.4 · (risk_awareness / 10)` |

where `det_all` is the fraction of all deterministic items that passed.

Then:

```
if partial_diagnosis:   s_k = min(s_k, 0.60)        # right area, wrong mechanism: borderline at best
elif rubric.misdiagnosis: s_k = min(s_k, 0.45)      # wrong root cause caps every node
obs_k = correct  if s_k >= 0.65  else incorrect
```

The partial-diagnosis branch lives in `Pedagogy._observations` (pedagogy.py), which wraps
`SkillGraph`: it computes `node_scores_from_grade` on a copy of the report with `misdiagnosis`
cleared, caps each score at `PARTIAL_DIAGNOSIS_CAP = 0.6`, thresholds with
`observation_for_score`, and applies those observations with `SkillGraph.update`. A node that
would otherwise score ≥ 0.60 therefore lands in the borderline band below (miss with softened
slip) instead of a full miss; a node that scored < 0.50 on its own stays `incorrect`.

**Partial credit.** BKT needs a binary observation. Rather than inventing a fractional update
(which breaks the generative model), we apply a *confidence-weighted* update only in the band
`0.50 ≤ s_k < 0.65`: treat it as `incorrect` but with the slip parameter raised to
`min(0.45, p_slip + 0.15)` for that one update, which softens the penalty. Everything below
0.50 uses the node's normal parameters. Everything ≥ 0.65 is `correct`. Document the band in
the UI as "borderline — counted as a miss with reduced penalty".

**Multiple submissions on the same case.** Only the first submission of a case counts as a
BKT observation for a node; resubmissions after seeing the revealed ground truth are practice,
not evidence (they would otherwise inflate `p` from retrieval of the answer rather than the
skill). Adversary turns never update BKT; they are feedback, not assessment.

### 2.3 Forgetting / decay

**Recommendation: apply a mild, time-based decay toward the prior, not toward zero, and only
when the node has not been observed for ≥ 21 days.** Classic BKT assumes no forgetting; that is
defensible within a tutoring session but not across months of sporadic self-study, which is
Axiom's usage pattern. Spaced-retrieval evidence (Roediger & Karpicke 2006) implies that
mastery inferred from a single success long ago should be discounted.

```
days_idle = today - last_observed_at
if days_idle > 21:
    lam = 0.015 per day beyond 21, capped at total shrink 0.5
    p = p_init + (p - p_init) * max(0.5, 1 - lam * (days_idle - 21))
```

Decay is applied lazily when the node is read for selection or display; it is never persisted
as an observation. The UI shows a decayed node with a hollow ring so the learner knows it is
"due".

### 2.4 Parameter choice and the degeneracy guard

Tiers map to presets (in `skill_dag.json → meta.bkt_presets`):

| Tier | p_init | p_transit | p_slip | p_guess | Used for |
|---|---|---|---|---|---|
| foundation | 0.35 | 0.25 | 0.10 | 0.15 | entry nodes with no prereqs (double entry, process flow, SELECT…) |
| core | 0.20 | 0.15 | 0.15 | 0.12 | most nodes |
| advanced | 0.10 | 0.10 | 0.20 | 0.08 | forensic detection, outbox/saga, recursive CTEs, LP… |

Justification:

- Corbett & Anderson (1995) fit skills in the ACT Programming Tutor with learning rates in the
  0.1–0.3 range and recommended bounding guess and slip to keep the model interpretable
  (secondary sources report their bounds as P(G) < 0.3, P(S) < 0.1).
- Baker, Corbett & Aleven (2008) define *model degeneracy* as parameter sets that violate the
  model's meaning, canonically when a student is predicted more likely to answer correctly
  *without* the skill than with it. The sufficient guard is `P(G) < 0.5` and `P(S) < 0.5`,
  together with `P(S) + P(G) < 1` so that a correct answer is always evidence *for* knowing. The
  DAG builder asserts all three for every node.
- Beck & Chang (2007) showed that different parameter sets can produce identical performance
  curves (identifiability); van de Sande (2013) showed the Markov chain depends on only three
  effective parameters. Our response is not to *fit* parameters from Axiom's sparse data, but to
  fix them by tier from the literature and treat them as priors. We do not run EM.
- Guess is lower than typical multiple-choice tutors because Axiom's observations come from
  open-ended analytical deliverables: it is hard to accidentally produce the right DSO and the
  right root cause. Slip is higher than Corbett & Anderson's 0.1 for advanced nodes because a
  single case can defeat a known skill through dirty-data accidents, which is why `advanced`
  uses 0.20.

### 2.5 Mastery and prerequisite unlocking

- **Mastered**: `p ≥ 0.95` (Corbett & Anderson's mastery criterion). Displayed as filled node.
- **Unlocked for targeting**: a node is eligible for case selection when every prerequisite has
  `p ≥ 0.60` (`meta.prereq_unlock_threshold`). This is deliberately lower than mastery: cases
  blend skills, and interleaving a partially known prerequisite with its dependent skill is
  itself beneficial (Rohrer et al. 2015). Nodes with no prerequisites are always eligible.
- The learner can override selection and pick any archetype ("targeted drill"); eligibility only
  governs the automatic picker.

### 2.6 Optional ELO alternative

Provide `EloTracker` behind the same interface for users who prefer a rating:

- Each node has rating `R_k` (start 1200). Each case has difficulty rating `D` =
  `{foundation: 1100, practitioner: 1300, principal: 1500}` plus `+50` per additional
  simultaneous anomaly.
- Expected score `E = 1 / (1 + 10^((D − R_k)/400))`; actual score `S = s_k` (the node score from
  §2.2, continuous). Update `R_k ← R_k + K · (S − E)` with `K = 40` for the first 5 observations
  on a node, then `K = 20`.
- Mastery display: `p_display = 1 / (1 + 10^((1500 − R_k)/400))`.

**Default is BKT**, because (a) it yields a calibrated probability, which the picker needs to
compute information gain; (b) its parameters are interpretable per node; (c) ELO's continuous
score hides the slip/guess structure that lets us give "borderline" feedback. ELO is kept
because it degrades more gracefully with difficulty-mismatched cases.

---

## 3. Case selection algorithm

Goal: choose the archetype (and difficulty) whose nodes are least known, subject to
prerequisites, with a bias toward nodes where one more observation is most informative.

```
def select_case(mastery, dag, rng_seed, recent_archetypes):
    # 1. eligibility
    eligible_nodes = { k for k in dag.nodes
                       if all(mastery[p] >= 0.60 for p in dag.prereqs[k]) }

    # 2. score each archetype
    best = []
    for arch, nodes in dag.archetype_nodes.items():
        usable = [k for k in nodes if k in eligible_nodes]
        if len(usable) < 2: continue                  # too much locked content
        # expected information gain of one observation on node k:
        #   H(p) - E_obs[H(p_post)]  (binary entropy, bits)
        gain = sum(info_gain(mastery[k], dag.bkt[k]) for k in usable)
        deficit = sum(1 - mastery[k] for k in usable)
        recency_penalty = 0.5 if arch in recent_archetypes[-3:] else 0.0
        score = 0.6 * deficit + 0.4 * gain - recency_penalty
        best.append((score, arch, usable))

    # 3. ties by seed so the same state proposes the same case
    best.sort(key=lambda t: (-round(t[0], 6), t[1]))
    top = [b for b in best if abs(b[0] - best[0][0]) < 1e-6]
    pick = top[SeededRNG(rng_seed).integers(len(top))]

    # 4. difficulty from mean mastery of the usable nodes
    m = mean(mastery[k] for k in pick.usable)
    difficulty = "foundation" if m < 0.35 else "practitioner" if m < 0.75 else "principal"
    return pick.arch, difficulty, pick.usable
```

`info_gain(p, prm)`: with `c = bkt_predict_correct(p, prm)`, compute `p_c = posterior if correct`,
`p_i = posterior if incorrect`, and return `H(p) − (c·H(p_c) + (1−c)·H(p_i))`. This favours
nodes near `p ≈ 0.5` and de-prioritises nodes already near 0 or 1 even if their deficit is large.
The 0.6/0.4 weighting keeps "lowest mastery" dominant, which is what the user intuitively
expects from "target my weakest nodes".

`recent_archetypes` is the last three case archetypes from the app DB; the penalty enforces
interleaving across pillars (Rohrer).

---

## 4. Difficulty scaling

The generator receives `difficulty` and scales the following. Everything is deterministic from
the seed; difficulty only changes parameter ranges.

| Dimension | foundation | practitioner | principal |
|---|---|---|---|
| Anomaly magnitude (fraction of the archetype's parameter range) | upper third (obvious) | middle third | lower third (subtle) plus one magnitude just inside tolerance noise |
| Simultaneous anomalies | 1 | 1 primary + 1 minor | 2 primary + 1 minor, interacting |
| Dirty-data intensity (§8 rules in ARCHITECTURE) | rates at 25% of max; no `tz_mixed`, no `unit_mixed` | 60% of max; all rules allowed | 100% of max; plus `late_arriving` and `orphan_fk` on the key table |
| Red herrings in dossier/timeline | 1 | 2 | 3, one of which is numerically supported |
| Dossier scaffolding | ask names the suspected area ("look at receivables"); data-room index annotates each table | ask names the symptom only; plain index | ask is a one-line board question; index lists tables with no annotation |
| Textbook availability | chapters available before submission | chapters available before submission | chapters unlocked only after first submission (productive failure) |
| Rubric strictness | anchors as written; `technical_execution` ceiling 8 unless all deterministic metrics pass | anchors as written | anchors as written; `business_judgment ≥ 8` requires quantified second-order effect; `risk_awareness ≥ 8` requires at least one red herring explicitly rejected with evidence |
| Adversary escalation ladder (§6.4) | stops at rung 3 | stops at rung 4 | full ladder, may escalate twice |
| Quarter simulation | 1 quarter | 2 quarters | 4 quarters with a shock event in quarter 2 |

---

## 5. The rubric

Four dimensions, each 0–10 integer. The LLM grader returns a score, evidence quotes from the
submission, and gaps. Scores are anchored; the grader prompt (§6.3) embeds the anchors verbatim.

### 5.1 Anchors

**technical_execution** — correctness and craft of the SQL/Python/model/diagram.

- **2** Code runs but answers a different question; joins fan out or aggregations double count;
  no handling of nulls/duplicates; metrics asserted without a derivation.
- **5** Core query/model is right in shape; at least one material data-quality issue missed
  (duplicates, timezone, units); derivation present but a step is wrong; no validation of row
  counts.
- **8** All asserted metrics reproducible from the evidence; data-quality issues found and
  handled explicitly; window/cohort/reconciliation logic idiomatic; at least one sanity check
  (row counts, tie-out to a control total).
- **10** Everything in 8 plus: the evidence is minimal and sufficient (no redundant queries),
  edge cases are named (right-censoring, cutoff boundaries), and a reviewer could rerun it
  unchanged.

**business_judgment** — did they find the root cause and reason about it like an operator?

- **2** Restates symptoms as causes; accepts a red herring; recommendation is generic ("improve
  collections").
- **5** Identifies the right area but not the mechanism; recommendation plausible but not tied
  to the quantified driver; no sizing of the impact.
- **8** Correct root cause with the mechanism explained; impact quantified in the company's
  own numbers; recommendation specific, sequenced, and owned by a named role.
- **10** Everything in 8 plus a quantified second-order consequence of the recommendation and
  an explicit statement of what would change their mind.

**communication** — can a CFO act on this in five minutes?

- **2** No clear answer in the first paragraph; numbers without units or periods; evidence not
  linked to claims.
- **5** Answer stated but buried; some claims linked to evidence; structure present but uneven.
- **8** Answer first, then evidence, then recommendation; every number has unit and period; each
  claim points at a query or model output.
- **10** Everything in 8 plus explicit confidence levels and the single chart/table a board
  would need.

**risk_awareness** — what could go wrong, and what did they rule out?

- **2** No alternatives considered; no mention of data limitations; recommendation ignores
  covenants/capacity/customers.
- **5** Mentions one alternative explanation or one risk, without evidence for ruling it out.
- **8** Rejects at least one red herring with evidence; names the operational or financial risk
  of the recommendation and a mitigation.
- **10** Everything in 8 plus a monitoring plan (which metric, which threshold, which cadence)
  and a pre-mortem of the recommendation.

### 5.2 Per-pillar sub-criteria (grader hints)

| Pillar | technical_execution looks for | business_judgment looks for |
|---|---|---|
| accounting_finance | statements tie; sub-ledger reconciled to GL; ratios computed with the right period denominators | accrual vs cash distinction; mechanism in journal-entry terms; covenant/liquidity impact |
| operations_strategy | Little's Law / queueing applied with consistent units; cohorts built correctly | constraint identified; second-order migration; unit-economics consequence |
| system_design | diagram shows the failure path and the fix; idempotency/outbox/replica reasoning specific | failure translated to P&L; blast radius; rollout risk |
| sql | correct joins/windows/cohorts; dedup and timezone handled; results reproducible | query results interpreted, not just shown |
| python | seeded, vectorised, validated; simulation reports percentiles | model assumptions tied to the case data |

### 5.3 Combining with the deterministic grade

The deterministic grader runs first and is authoritative for numbers. Its result constrains the
rubric:

```
det_pass = passed_items / total_items   (metrics + reference-query signatures)
ceiling_te = 10 if det_pass >= 0.9
           = 8  if det_pass >= 0.6
           = 5  if det_pass >= 0.3
           = 3  otherwise
rubric.technical_execution = min(rubric.technical_execution, ceiling_te)
if any asserted metric is "unsupported" in the grader's metric_support (or names an evidence
   item that is not in the submission):
    rubric.technical_execution = min(rubric.technical_execution, 5)
if any critical anomaly's expected metric failed and rubric.misdiagnosis == False:
    rubric.business_judgment = min(rubric.business_judgment, 6)
overall = 0.35*te + 0.35*bj + 0.15*comm + 0.15*risk   (0–10, one decimal)
```

The clamp is applied in code after the LLM returns, and the GradeReport records both the raw
LLM score and the clamped score so the learner can see the cap.

---

## 6. Prompt designs

All prompts are Python `str.format` templates: placeholders are `{snake_case}` and literal
braces in JSON examples are doubled (`{{ }}`). Each prompt has a `SYSTEM` and a `USER` part.
Every prompt ends with the same anti-hallucination block, defined once. Its cents line is
backed by the context: every mapping passed to a prompt goes through `pedagogy.with_usd`, which
keeps each `<name>_cents` key and adds a sibling `<name>_usd` (dollars, 2 decimals), and for
objects whose `unit` is cents adds `value_usd` / `min_usd` / `max_usd` / `default_usd` /
`expected_usd` / `submitted_usd`. The narrator's `LEVER DEFINITIONS` also carry each lever's
`lag_periods` and `moved_kpis_this_quarter`, which is what lets it name the decision that had no
measurable effect yet.

```
ANTI_HALLUCINATION = """
NUMERIC DISCIPLINE
- Every number you state must appear verbatim in the data you were given, or be a simple
  arithmetic combination of such numbers that you show inline (e.g. "DSO rose from 41 to 68
  days (+27)").
- If you need a number you do not have, write "[not provided]" and do not estimate it.
- When both a cents and a dollars figure exist for the same quantity, state dollars with the
  unit, never raw cents (write "$8.5M", not "852,487,846").
- Never invent names, dates, tables, columns, customers or events that are not in the input.
- Output ONLY the JSON object requested. No prose before or after. No markdown fences.
"""
```

### 6.1 `case_synthesizer`

**SYSTEM**

```
You are a Harvard Business School case writer with a forensic accountant's discipline. You
write immersive, executive-level case dossiers from a structured seed. Your dossier must let a
strong analyst discover the root cause from the data room; it must never state the root cause.

HARD RULES
1. REVEAL: every item in `reveal` must appear in the dossier, in substance.
2. CONCEAL: nothing in `conceal` may appear, be paraphrased, be hinted at by naming the
   anomaly mechanism, or be implied by a stakeholder "suspecting" it. Stakeholders may be
   wrong, worried, or defensive, but they do not diagnose.
3. Red-herring timeline events are presented with the same seriousness as real ones.
4. Stakeholders speak in their own voice (email, Slack, board-deck excerpt). Each one knows
   only what `knows` says and conceals what `hides` says; they may be evasive.
5. The ask is ambiguous at the stated difficulty: foundation names the area, practitioner
   names the symptom, principal asks a one-line board question.
6. Write in present tense, plain business English, no jargon the company would not use.
7. Length budget per section: `situation` <= 250 words; `company_background` <= 300 words;
   each stakeholder artifact `body` 80-180 words; `the_ask` <= 120 words.
8. At practitioner and principal, do not quote the multi-quarter trajectory of any metric that
   is itself an ask or an anomaly symptom; quote the latest value only. (The asks are the
   metrics listed in DELIVERABLE EXPECTED.)
9. Stakeholder artifacts must disagree with each other on at least one fact that the data room
   can settle; make the disagreement explicit.
""" + ANTI_HALLUCINATION
```

**USER**

```
COMPANY
{company_json}

INDUSTRY NARRATIVE HOOKS
{narrative_hooks_json}

PERIODS: {periods_json}
HEADLINE KPIs BY PERIOD (public view, safe to quote)
{kpis_json}

DOSSIER SEED
reveal: {reveal_json}
conceal: {conceal_json}
stakeholders: {stakeholders_json}
timeline: {timeline_json}
artifacts requested: {artifacts_json}
architecture diagram (mermaid, may be null): {architecture_mermaid}

DATA ROOM (tables available to the analyst)
{table_index_json}

DIFFICULTY: {difficulty}
DELIVERABLE EXPECTED: {deliverable_spec}

Return JSON:
{{
  "title": "string (<= 12 words, no company name leak of the mechanism)",
  "situation": "markdown, 2-3 paragraphs, <= 250 words: where the company is today and why this landed on the analyst's desk",
  "company_background": "markdown, 2 paragraphs, <= 300 words, grounded in the COMPANY and NARRATIVE HOOKS",
  "stakeholder_artifacts": [
    {{"stakeholder": "name", "role": "role", "format": "email|slack|board_deck|memo",
      "subject": "string", "body": "markdown in the stakeholder's voice, 80-180 words"}}
  ],
  "timeline": [{{"period": "2024Q3", "headline": "string", "detail": "string"}}],
  "the_ask": "markdown, <= 120 words: the ambiguous question, scaled to DIFFICULTY",
  "deliverable_spec": "markdown bullet list of what the memo must contain (echo DELIVERABLE EXPECTED)",
  "data_room_index": [{{"table": "name", "note": "one line; at foundation difficulty describe what the table holds, at principal leave note empty"}}],
  "architecture_mermaid": "string or null, passed through unchanged"
}}
```

Post-processing (§7 leak check) runs on the concatenated text of all string fields.

### 6.2 `textbook_chapter`

**SYSTEM**

```
You are writing one chapter of a first-principles technical textbook. The chapter teaches ONE
skill node and uses the live case's own numbers as its worked example. You explain mechanisms,
not recipes. You assume a smart reader who has never seen the topic.

STRUCTURE (markdown, 1200-2000 words)
1. "Why this exists" — the problem the idea solves, in one paragraph.
2. "The model" — the core formula or pattern, derived not asserted. Use LaTeX ($...$) for math.
3. "Worked example from this case" — apply it to the numbers provided; show each step.
4. "Syntax patterns" — for SQL/Python nodes: 2-3 idiomatic snippets (DuckDB SQL / pandas) that
   run against the tables listed; for business nodes: the spreadsheet/ledger layout.
5. "Where people go wrong" — 3-5 named failure modes with how to detect them.
6. "Self-check" — exactly 3 questions; answers in a separate field, never in the chapter body.
   Question 3 must be numeric: it uses the case's numbers and its answer is a single number
   with a unit that the reader can check.

RULES
- Do not mention the case's root cause or any anomaly; teach the skill, not the answer.
- Use only tables/columns from the provided schema.
- Every number in the worked example must come from the provided data.
- Derive each formula from a conservation law or a definition, then show one limiting case where
  it breaks.
- For SQL, show the wrong and the right version of one query and name the error (fan-out,
  timezone, censoring, double counting).
""" + ANTI_HALLUCINATION
```

**USER**

```
SKILL NODE: {node_id} — {node_title}
SUMMARY: {node_summary}
MENTAL MODELS TO CONVEY: {mental_models_json}
PREREQUISITES THE READER HAS: {prereq_titles_json}
PILLAR: {pillar}

CASE CONTEXT (public view)
company: {company_json}
periods: {periods_json}
relevant public KPIs: {kpis_json}
schema excerpt (tables relevant to this node): {schema_excerpt_json}
sample rows: {sample_rows_json}

Return JSON:
{{
  "node_id": "{node_id}",
  "title": "string",
  "chapter_markdown": "string",
  "self_check": [{{"question": "string (question 3 numeric, using the case's numbers)", "answer": "string"}}],
  "snippets": [{{"language": "sql|python", "purpose": "string", "code": "string"}}]
}}
```

### 6.3 `grader`

**SYSTEM**

```
You are an adversarial grader: a former Big Four forensic partner and a staff engineer sharing
one chair. You have the ground truth. The learner does not. You grade the submission against
the anchored rubric below. You are fair but unforgiving about unsupported claims.

PROCESS
1. Read the DETERMINISTIC GRADE first. It is authoritative for every number. Do not re-derive
   numbers; do not dispute it.
2. Decide `misdiagnosis`: true if the memo's stated root cause is not the GROUND TRUTH root
   cause or is one of the RED HERRINGS. Decide `partial_diagnosis`: true if the memo names the
   right area (the right account, process, cohort or component) but the wrong mechanism; a
   partial diagnosis is still a misdiagnosis, so set both. A red herring is never partial.
3. For each asserted metric, name the evidence item (by its name) that produces it, or mark it
   unsupported; unsupported metrics cannot earn technical_execution above 5.
4. Score each dimension using the anchors. Quote the submission as evidence (exact substrings,
   <= 200 chars each). List gaps as concrete, checkable statements.
5. Name the strongest and weakest point in one sentence each.
6. Recommend 1-3 `next_drill_nodes` from the CANDIDATE NODES list only.

RUBRIC ANCHORS
{rubric_anchors}

PER-PILLAR HINTS
{pillar_hints}

RULES
- Evidence quotes must be verbatim substrings of the submission.
- Do not award technical_execution above the CEILING given.
- If the submission asserts a number the deterministic grade marked out of tolerance, cite it
  as a gap.
- Be specific: "did not deduplicate invoices before summing" beats "data quality issues".
""" + ANTI_HALLUCINATION
```

**USER**

```
CASE: {archetype} / {difficulty}
GROUND TRUTH
root_cause: {root_cause}
anomalies: {anomalies_json}
red_herrings: {red_herrings_json}
expected_metrics: {expected_metrics_json}

DETERMINISTIC GRADE
{deterministic_grade_json}
technical_execution CEILING: {ceiling_te}

SUBMISSION
memo_markdown:
{memo_markdown}
asserted metrics: {metrics_json}
sql evidence: {sql_evidence_json}
python evidence: {python_evidence_json}
design_mermaid: {design_mermaid}
financial_model: {financial_model_json}

CANDIDATE NODES: {candidate_nodes_json}

Return JSON:
{{
  "dimensions": {{
    "technical_execution": {{"score": 0, "evidence": ["string"], "gaps": ["string"]}},
    "business_judgment":   {{"score": 0, "evidence": ["string"], "gaps": ["string"]}},
    "communication":       {{"score": 0, "evidence": ["string"], "gaps": ["string"]}},
    "risk_awareness":      {{"score": 0, "evidence": ["string"], "gaps": ["string"]}}
  }},
  "overall": 0.0,
  "misdiagnosis": false,
  "partial_diagnosis": false,
  "metric_support": [{{"metric": "key from asserted metrics", "evidence": "name of the sql/python evidence item, or unsupported"}}],
  "stated_root_cause": "string: what the learner claimed, in one sentence",
  "strongest_point": "string",
  "weakest_point": "string",
  "next_drill_nodes": ["node.id"]
}}
```

Code recomputes `overall` from the clamped scores (§5.3) and ignores the LLM's `overall` except
for logging drift.

### 6.4 `adversary`

Persona by primary pillar: `accounting_finance → CFO`, `operations_strategy → COO or
skeptical PE associate`, `system_design → VP of Engineering`, `sql/python → Head of Data`. The
persona is fixed for the whole conversation on a submission.

Escalation ladder (`challenge_type`), one rung per turn unless the learner's reply fully
answers the rung:

1. `clarify` — ask them to restate the claim precisely (which metric, which period).
2. `probe_assumption` — attack an assumption the memo made silently.
3. `counter_evidence` — present a number from the data that superficially contradicts them
   (drawn from RED HERRINGS or SOCRATIC TRAPS).
4. `second_order` — ask what their recommendation does to another part of the business next
   quarter (use DECISION LEVERS).
5. `concede_or_escalate` — if their rebuttals held, concede explicitly; otherwise state the
   unresolved gap and end.

**SYSTEM**

```
You are {persona_name}, {persona_role} at {company_name}. You are reviewing an analyst's memo
in a live meeting. You are skeptical, busy, and fair. You have your own agenda: {persona_agenda}.
You know the data the analyst has. You do NOT know the ground truth and must never state it;
you only test the analyst's reasoning. You may be wrong on purpose when you push a red herring.

STYLE: 60-140 words per turn, first person, no bullet lists, one challenge per turn, end with a
question unless you are conceding. Cite specific numbers from the data when you challenge.

MEMORY: Do not repeat a challenge the analyst already answered with evidence; reference their
evidence by name.

LADDER: follow the escalation ladder; your `challenge_type` must be the current rung unless the
analyst fully answered the previous rung, in which case advance. At the final rung either
concede (if the analyst's rebuttals were evidence-backed) or escalate the unresolved gap.
At rung 3 (counter_evidence), cite one number from the public KPIs that superficially
contradicts the analyst's thesis.
When you concede, say so visibly in one line that names what you were wrong about (e.g. "I was
wrong about seasonality.") and set `concedes` true.
MAX RUNG FOR THIS DIFFICULTY: {max_rung}
""" + ANTI_HALLUCINATION
```

**USER**

```
CURRENT RUNG: {rung}
GROUND-TRUTH MATERIAL FOR CHALLENGES (never reveal directly)
red_herrings: {red_herrings_json}
socratic_traps (challenge + correct rebuttal): {socratic_traps_json}
decision_levers: {levers_json}
expected_metrics (for citing numbers): {expected_metrics_json}
public KPIs: {kpis_json}

GRADE SUMMARY: overall {overall}, misdiagnosis {misdiagnosis}, weakest point: {weakest_point}

MEMO (analyst): {memo_markdown}
CONVERSATION SO FAR: {history_json}
ANALYST'S LATEST REPLY: {latest_reply}

Return JSON:
{{
  "persona": "{persona_name}",
  "message": "string",
  "challenge_type": "clarify|probe_assumption|counter_evidence|second_order|concede_or_escalate",
  "cites": ["metric keys from expected_metrics or public KPIs used in the message"],
  "concedes": false,
  "rung_advanced": true
}}
```

Code validates `cites` against the known keys and strips unknown ones.

### 6.5 `quarter_narrator`

**SYSTEM**

```
You are the CFO writing the one-page board update for the quarter that just closed. The
numbers were produced by a deterministic simulation; your job is to narrate them, attribute
changes to the decisions taken, and flag what the board should watch. You never add numbers.

STRUCTURE (markdown, 250-400 words): headline (one sentence), what we did (the decisions),
what happened (KPI deltas with before → after), what it cost / what it earned, what we watch
next quarter.
State explicitly which decision had no measurable effect this quarter and why (lag).
""" + ANTI_HALLUCINATION
```

**USER**

```
COMPANY: {company_name}
PERIOD CLOSED: {period}
DECISIONS TAKEN (lever → value): {decisions_json}
LEVER DEFINITIONS: {levers_json}
KPIs PRIOR PERIOD: {kpis_prev_json}
KPIs THIS PERIOD: {kpis_now_json}
STATEMENT HEADLINES (revenue, gross profit, operating income, cash, DSO, DIO, DPO): {headlines_json}
SHOCK EVENT THIS PERIOD (may be null): {shock_json}
UNRESOLVED ANOMALY STILL ACTIVE: {anomaly_active}

Return JSON:
{{
  "narrative_markdown": "string",
  "watch_items": [{{"metric": "key", "threshold": "string", "why": "string"}}]
}}
```

---

## 7. Leak check

After `case_synthesizer` and `textbook_chapter` return, run `leak_scan(text, world)`:

1. **Concealed strings.** For each item in `dossier_seed.conceal`, build a normalised form
   (lowercase, strip punctuation, collapse whitespace). Flag if the normalised text contains it,
   or contains ≥ 70% of its content tokens (length ≥ 4) within any 12-token window.
2. **Anomaly vocabulary.** For each `Anomaly.kind` in ground truth, flag if the kind's key words
   (from `forensic_patterns.json → signatures`, split and stemmed) co-occur with "cause",
   "because", "due to", "driven by" within a sentence.
3. **Ground-truth numbers.** For each `ExpectedMetric.value` and each `Anomaly.magnitude`
   value, flag if the text contains the number formatted to 2 or 3 significant figures (as
   percent, decimal, or with a currency symbol) **unless** that number also appears in the
   public KPI set (then it is legitimately quotable).
4. **Stakeholder knowledge.** For each stakeholder artifact, flag if its body contains any item
   from that stakeholder's `hides`.

Policy: on any flag, regenerate once with the flagged snippets appended to the user prompt as
`DO NOT INCLUDE: [...]`. If the second attempt still leaks, keep the second output, redact the
flagged spans with `[redacted]`, and record `leak_check: {attempts: 2, redactions: n}` in
`case_artifacts`. The UI shows a small "redacted for fairness" marker. Never return an
unscanned dossier.

---

## 8. Evaluating the system offline

`FakeTransport` (ARCHITECTURE §9) returns canned JSON per `purpose`. Tests must cover:

1. **Parsing robustness.** Grader output wrapped in ```json fences, with trailing prose, with
   a missing `next_drill_nodes` key → extractor + one repair round-trip yields a valid
   `RubricGrade` or raises `LLMOutputError` with the raw text attached.
2. **Clamping.** Given `det_pass = 0.5` and LLM `technical_execution = 9`, the report shows raw
   9 and clamped 8; `overall` recomputed.
3. **BKT updates from fixtures.** Load each fixture in `engine/pedagogy_fixtures/`, run the
   deterministic grader against the matching archetype at seed 1, feed the fixture's
   `fake_rubric` through FakeTransport, and assert:
   - `good_submission_working_capital`: every node in `archetype_nodes.working_capital_crunch`
     receives `correct`; `p` strictly increases; overall within `expected_rubric_range`.
   - `bad_submission_forensic_revenue`: `misdiagnosis == true`; all node scores ≤ 0.45; every
     node receives `incorrect`; `p` strictly decreases from prior.
   - `good_submission_outbox`: system_design nodes `correct`; `sql.reconciliation_queries`
     `correct`; `accounting_finance.subledger_reconciliation` falls in the borderline band
     (0.50–0.65) and is updated with the softened slip.
4. **Selection determinism.** Same mastery vector + same seed → same archetype twice; a
   different seed changes only tie-breaks.
5. **Leak scan.** A dossier fixture containing a concealed phrase, a paraphrase at 70% token
   overlap, and a ground-truth number formatted as a percent → three flags; the same number
   present in public KPIs → no flag.
6. **Decay.** A node observed 60 days ago at `p = 0.9` with `p_init = 0.2` reads back at
   `0.2 + 0.7·max(0.5, 1 − 0.015·39) = 0.2 + 0.7·0.5 = 0.55` (shrink capped), while a node
   observed 10 days ago is unchanged.

A good submission, concretely, has: the right root cause stated in the first paragraph; every
asserted metric within tolerance; SQL that deduplicates by business key before aggregating and
handles the timezone column; a recommendation with an owner and a monitored metric; at least one
red herring rejected with a number. A bad submission accepts a red herring as the cause, asserts
a headline metric from a fan-out join (so it fails tolerance), and recommends a generic action.
The fixtures encode exactly these.

---

## 9. Research grounding

- **Bayesian Knowledge Tracing.** Corbett, A. T. & Anderson, J. R. (1995). Knowledge tracing:
  Modeling the acquisition of procedural knowledge. *User Modeling and User-Adapted
  Interaction*, 4, 253–278. The four-parameter model, the two-step update, and the 0.95 mastery
  criterion come from here.
- **Contextual guess/slip and degeneracy.** Baker, R. S. J. d., Corbett, A. T. & Aleven, V.
  (2008). More accurate student modeling through contextual estimation of slip and guess
  probabilities in Bayesian Knowledge Tracing. *ITS 2008*, LNCS 5091, 406–415.
  https://link.springer.com/doi/10.1007/978-3-540-69132-7_44 — defines model degeneracy; our
  `P(S), P(G) < 0.5` and `P(S)+P(G) < 1` guards follow its argument.
- **Identifiability.** Beck, J. E. & Chang, K. (2007). Identifiability: A fundamental problem
  of student modeling. *UM 2007*. And van de Sande, B. (2013). Properties of the Bayesian
  Knowledge Tracing model. *JEDM* 5(2), 1–10.
  https://jedm.educationaldatamining.org/index.php/JEDM/article/download/35/pdf_27 — why we fix
  parameters by tier rather than fit them from sparse data. Doroudi & Brunskill (2017) argue BKT
  is identifiable under mild conditions; either way, fixed interpretable priors are the right
  call at Axiom's data volume.
- **Two-sigma.** Bloom, B. S. (1984). The 2 sigma problem. *Educational Researcher* 13(6), 4–16.
  The motivation for one-on-one Socratic tutoring (the adversary pane) rather than static
  feedback.
- **Self-explanation.** Chi, M. T. H., Bassok, M., Lewis, M. W., Reimann, P. & Glaser, R.
  (1989). Self-explanations: How students study and use examples in learning to solve problems.
  *Cognitive Science* 13, 145–182. Why the grader demands a mechanism, not a label, and why the
  textbook's worked example uses the live case.
- **Productive failure.** Kapur, M. (2008). Productive failure. *Cognition and Instruction*
  26(3), 379–424; and Sinha & Kapur (2021) meta-analysis of 166 comparisons favouring
  problem-first sequencing. Why principal difficulty unlocks chapters only after the first
  attempt.
- **Case method.** Garvin, D. A. (2003). Making the case: Professional education for the world
  of practice. *Harvard Magazine*, Sept–Oct 2003. The dossier structure (situation, protagonist,
  ambiguous decision, data exhibits) and the cold-call style of the adversary.
- **Deliberate practice.** Ericsson, K. A., Krampe, R. T. & Tesch-Römer, C. (1993). The role
  of deliberate practice in the acquisition of expert performance. *Psychological Review* 100(3),
  363–406. Why selection targets the edge of competence and why feedback is immediate and
  specific.
- **Retrieval practice.** Roediger, H. L. & Karpicke, J. D. (2006). Test-enhanced learning.
  *Psychological Science* 17(3), 249–255. Why resubmissions after reveal do not count as
  evidence, and why decay is applied to unobserved nodes.
- **Interleaving.** Rohrer, D., Dedrick, R. F. & Stershic, S. (2015). Interleaved practice
  improves mathematics learning. *Journal of Educational Psychology* 107(3), 900–908; Rohrer,
  Dedrick & Burgess (2014). Why cases blend pillars and why the picker penalises recent
  archetypes. Note the replication record is mixed (a Kutztown statistics replication found a
  blocked advantage on early tests), which is why interleaving is a soft penalty, not a hard
  rule.

---

## 10. Implementation notes for the engineer

- `skill_graph.py` exposes: `load_dag()`, `BKTParams`, `bkt_update`, `bkt_predict_correct`,
  `info_gain`, `node_scores_from_grade(grade, archetype_nodes) -> dict[node, float]`,
  `observations_from_scores(scores) -> dict[node, (correct: bool, slip_override: float|None)]`,
  `apply_decay(mastery_row, today)`, `select_case(...)`, and an `EloTracker` with the same
  `observe()` interface.
- Persist per node: `p_known`, `attempts`, `last_observed_at`, `history` (JSON list of
  `(case_id, s_k, obs, p_before, p_after)`), `elo_rating`.
- `archetype_nodes` in the DAG is the single source of truth for which nodes a case updates;
  `CaseWorld.skill_nodes` must be populated from it at generation time.
- The rubric anchors in §5.1 and the pillar hints in §5.2 are embedded into the grader prompt
  from constants, not retyped; keep them in `pedagogy.py` as `RUBRIC_ANCHORS` and
  `PILLAR_HINTS` so `/settings/prompts` shows the exact text the model sees.
