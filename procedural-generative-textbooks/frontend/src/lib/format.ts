const compact = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });
const int = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const two = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });
const usd2 = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

/** "Not meaningful": rendered for null ratios, the legacy 999.0 sentinel, and non-finite values. */
export const NM = "n/m";

/** Legacy backend sentinel for an undefined ratio (interest coverage with zero interest expense). */
const RATIO_SENTINEL = 999;

function finite(x: number | null | undefined): x is number {
  return typeof x === "number" && Number.isFinite(x);
}

export function fmtCents(cents: number, opts: { compact?: boolean } = {}): string {
  if (!finite(cents)) return NM;
  return fmtUsd(cents / 100, opts.compact ?? false);
}

export function fmtUsd(usd: number, compactForm = true): string {
  if (!finite(usd)) return NM;
  const sign = usd < 0 ? "−" : "";
  return `${sign}$${compactForm ? compact.format(Math.abs(usd)) : int.format(Math.abs(usd))}`;
}

/** Dollars with cents, e.g. "$1,200,000.00" (echo of a parsed USD entry). */
export function fmtUsdExact(usd: number): string {
  if (!finite(usd)) return NM;
  return `${usd < 0 ? "−" : ""}$${usd2.format(Math.abs(usd))}`;
}

export function fmtPct(x: number, digits = 1): string {
  if (!finite(x)) return NM;
  return `${(x * 100).toFixed(digits)}%`;
}

export function fmtNum(x: number): string {
  if (!finite(x)) return NM;
  return Math.abs(x) >= 10_000 ? compact.format(x) : two.format(x);
}

/** True when a key/unit pair is denominated in cents (`*_cents` key or unit "cents"). */
export function isCents(key: string, unit = ""): boolean {
  return /_cents$/i.test(key) || /^cents?$/i.test(unit.trim());
}

const USD_SCALE: Record<string, number> = { k: 1e3, thousand: 1e3, m: 1e6, mm: 1e6, mn: 1e6, million: 1e6, b: 1e9, bn: 1e9, billion: 1e9 };

/**
 * Parse a dollar amount as people type it: "1.2M", "$850,000", "850k", "-$1.5bn", "(2,400)",
 * "3.1 million", "USD 40000". Returns dollars, or null when the text is not a single amount.
 */
export function parseUsd(input: string): number | null {
  let s = input.trim().toLowerCase().replace(/usd/g, "").replace(/[\s,_]/g, "");
  if (s === "") return null;
  let sign = 1;
  if (s.startsWith("(") && s.endsWith(")")) {
    sign = -1;
    s = s.slice(1, -1);
  }
  if (/^[-−–]/.test(s)) {
    sign *= -1;
    s = s.slice(1);
  }
  if (s.startsWith("$")) s = s.slice(1);
  const m = /^(\d+(?:\.\d*)?|\.\d+)(k|thousand|mm|mn|million|m|bn|billion|b)?$/.exec(s);
  if (!m) return null;
  const v = Number(m[1]) * (m[2] ? USD_SCALE[m[2]] : 1) * sign;
  return Number.isFinite(v) ? v : null;
}

/** Whole cents from a dollar amount (rounded half away from zero). */
export function usdToCents(usd: number): number {
  return Math.sign(usd) * Math.round(Math.abs(usd) * 100);
}

/**
 * A financial ratio: null, the legacy 999.0 sentinel and non-finite values render "n/m";
 * a key absent for the period renders "—".
 */
export function fmtRatio(key: string, v: number | null | undefined): string {
  if (v === undefined) return "—";
  if (v === null || !Number.isFinite(v) || Math.abs(v) >= RATIO_SENTINEL) return NM;
  if (/margin|pct/.test(key)) return fmtPct(v);
  if (/coverage|leverage/.test(key)) return `${v.toFixed(1)}×`;
  return v.toFixed(1);
}

/** Format a value according to a contract unit string ("usd", "cents", "pct", "days", "months", "ratio", "bool", "x", "count"). */
export function fmtUnit(value: number | null | undefined, unit: string): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  if (!Number.isFinite(value)) return NM;
  switch (unit) {
    case "usd":
      return fmtUsd(value);
    case "cents":
      return fmtCents(value, { compact: true });
    case "pct":
      return fmtPct(value);
    case "days":
      return `${two.format(value)}d`;
    case "x":
      return `${two.format(value)}×`;
    case "months":
      return `${two.format(value)} mo`;
    case "bool":
      return value ? "yes (1)" : "no (0)";
    default:
      return fmtNum(value);
  }
}

/** A metric/ask value: cents-denominated ones (by key or unit) as full dollars, others by unit. */
export function fmtMetric(value: number | null | undefined, unit: string, key = ""): string {
  if (finite(value) && isCents(key, unit)) return fmtCents(value);
  return fmtUnit(value, unit);
}

/** Guess a unit from a KPI key name such as "cash_usd", "cash_cents" or "churn_monthly". */
export function unitFromKey(key: string): string {
  if (/_cents$/.test(key)) return "cents";
  if (/_usd$|revenue|cash/.test(key)) return "usd";
  if (/per_(hour|day|week|month)$/.test(key)) return "number";
  if (/utilization|pct|churn|nrr|rate|margin|share/.test(key)) return "pct";
  if (/dso|dpo|dio|days/.test(key)) return "days";
  return "number";
}

export function humanize(key: string): string {
  return key.replace(/[_.]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export function relTime(iso: string): string {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const s = (Date.now() - t) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}
