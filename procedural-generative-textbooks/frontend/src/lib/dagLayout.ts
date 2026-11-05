import { PILLARS, type Pillar, type SkillGraph, type SkillNode } from "../api/types";

export interface PlacedNode {
  node: SkillNode;
  x: number;
  y: number;
  layer: number;
}

export interface DagLayout {
  nodes: Map<string, PlacedNode>;
  columns: { pillar: Pillar; x: number; width: number }[];
  width: number;
  height: number;
}

export const LAYOUT = {
  colWidth: 232,
  colGap: 12,
  rowHeight: 31,
  indent: 13,
  top: 36,
  left: 16,
} as const;

/**
 * Layered layout by pillar columns.
 * 1. Layer = longest path from a root through same-pillar prerequisites.
 * 2. Within a pillar, nodes are ordered by layer, then by barycenter of their parents' rows
 *    (one sweep, top-down), which keeps children close to their parents.
 * 3. Each node gets its own row; x is indented by layer, so the column reads as an outline.
 */
export function layoutDag(graph: SkillGraph): DagLayout {
  const byId = new Map(graph.nodes.map((n) => [n.id, n]));
  const layerOf = new Map<string, number>();

  const layer = (id: string, seen: Set<string> = new Set()): number => {
    const cached = layerOf.get(id);
    if (cached !== undefined) return cached;
    const n = byId.get(id);
    if (!n || seen.has(id)) return 0;
    seen.add(id);
    const local = n.prereqs.filter((p) => byId.get(p)?.pillar === n.pillar);
    const l = local.length ? 1 + Math.max(...local.map((p) => layer(p, seen))) : 0;
    layerOf.set(id, l);
    return l;
  };
  graph.nodes.forEach((n) => layer(n.id));

  const placed = new Map<string, PlacedNode>();
  const columns: DagLayout["columns"] = [];
  let maxRows = 0;

  PILLARS.forEach((pillar, ci) => {
    const x0 = LAYOUT.left + ci * (LAYOUT.colWidth + LAYOUT.colGap);
    columns.push({ pillar, x: x0, width: LAYOUT.colWidth });
    const members = graph.nodes.filter((n) => n.pillar === pillar);
    const maxLayer = Math.max(0, ...members.map((n) => layerOf.get(n.id) ?? 0));
    const row = new Map<string, number>();
    const ordered: SkillNode[] = [];
    for (let l = 0; l <= maxLayer; l += 1) {
      const inLayer = members
        .filter((n) => (layerOf.get(n.id) ?? 0) === l)
        .map((n, i) => {
          const parentRows = n.prereqs.map((p) => row.get(p)).filter((r): r is number => r !== undefined);
          const bary = parentRows.length ? parentRows.reduce((a, b) => a + b, 0) / parentRows.length : i;
          return { n, bary };
        })
        .sort((a, b) => a.bary - b.bary);
      for (const { n } of inLayer) {
        row.set(n.id, ordered.length);
        ordered.push(n);
      }
    }
    ordered.forEach((n, i) => {
      const l = layerOf.get(n.id) ?? 0;
      placed.set(n.id, { node: n, layer: l, x: x0 + 10 + l * LAYOUT.indent, y: LAYOUT.top + 12 + i * LAYOUT.rowHeight });
    });
    maxRows = Math.max(maxRows, ordered.length);
  });

  return {
    nodes: placed,
    columns,
    width: LAYOUT.left * 2 + PILLARS.length * LAYOUT.colWidth + (PILLARS.length - 1) * LAYOUT.colGap,
    height: LAYOUT.top + maxRows * LAYOUT.rowHeight + 16,
  };
}

/** Edge path: elbow curve within a column, S-curve across columns. */
export function edgePath(a: PlacedNode, b: PlacedNode): string {
  if (a.node.pillar === b.node.pillar) {
    const x = a.x;
    return `M ${x} ${a.y + 6} C ${x} ${b.y - 2}, ${x} ${b.y}, ${b.x - 7} ${b.y}`;
  }
  const sx = a.x + 6;
  const tx = b.x - 7;
  const mx = (sx + tx) / 2;
  return `M ${sx} ${a.y} C ${mx} ${a.y}, ${mx} ${b.y}, ${tx} ${b.y}`;
}
