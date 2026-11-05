import type { Ask } from "../api/types";
import { isCents, parseUsd, usdToCents } from "./format";

/** Asks entered in dollars: `usd` asks, and cents asks (key `*_cents` or unit "cents"), which are converted on submit. */
export function askInDollars(a: Pick<Ask, "key" | "unit">): boolean {
  return a.unit === "usd" || isCents(a.key, a.unit);
}

/**
 * The value the backend expects for a typed ask, or null when empty or unparseable.
 * Dollar asks accept "1.2M", "$850,000", "850k"; cents asks are sent as whole cents.
 * Percentages accept 31 or 0.31.
 */
export function parseAsk(a: Pick<Ask, "key" | "unit">, raw: string): number | null {
  const text = raw.trim();
  if (text === "") return null;
  if (askInDollars(a)) {
    const usd = parseUsd(text);
    if (usd === null) return null;
    return isCents(a.key, a.unit) ? usdToCents(usd) : usd;
  }
  const n = Number(text.replace(/[$,%\s]/g, ""));
  if (Number.isNaN(n)) return null;
  return a.unit === "pct" && n > 1 ? n / 100 : n;
}
