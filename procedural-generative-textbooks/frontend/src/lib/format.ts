const compact = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });
const int = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const two = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });

export function fmtCents(cents: number, opts: { compact?: boolean } = {}): string {
  const usd = cents / 100;
  const sign = usd < 0 ? "−" : "";
  return `${sign}$${opts.compact ? compact.format(Math.abs(usd)) : int.format(Math.abs(usd))}`;
}

export function fmtUsd(usd: number, compactForm = true): string {
  const sign = usd < 0 ? "−" : "";
  return `${sign}$${compactForm ? compact.format(Math.abs(usd)) : int.format(Math.abs(usd))}`;
}

export function fmtPct(x: number, digits = 1): string {
  return `${(x * 100).toFixed(digits)}%`;
}

export function fmtNum(x: number): string {
  return Math.abs(x) >= 10_000 ? compact.format(x) : two.format(x);
}

/** Format a value according to a contract unit string ("usd", "pct", "days", "ratio", "x", "count"). */
export function fmtUnit(value: number | null | undefined, unit: string): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
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
    default:
      return fmtNum(value);
  }
}

/** Guess a unit from a KPI key name such as "cash_usd" or "churn_monthly". */
export function unitFromKey(key: string): string {
  if (/_usd$|revenue|cash/.test(key)) return "usd";
  if (/pct|churn|nrr|rate|margin|share/.test(key)) return "pct";
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
