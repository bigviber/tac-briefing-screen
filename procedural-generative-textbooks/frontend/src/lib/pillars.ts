import type { Pillar } from "../api/types";

export const PILLAR_COLOR: Record<Pillar, string> = {
  accounting_finance: "#f5b74e",
  operations_strategy: "#3ecf8e",
  system_design: "#b78cff",
  sql: "#5b8def",
  python: "#ff8a5b",
};

export const PILLAR_LABEL: Record<Pillar, string> = {
  accounting_finance: "Accounting & Finance",
  operations_strategy: "Ops & Strategy",
  system_design: "System Design",
  sql: "SQL",
  python: "Python",
};

export const PILLAR_SHORT: Record<Pillar, string> = {
  accounting_finance: "A&F",
  operations_strategy: "OPS",
  system_design: "SYS",
  sql: "SQL",
  python: "PY",
};

/** Tailwind text classes per pillar (static strings so JIT keeps them). */
export const PILLAR_TEXT: Record<Pillar, string> = {
  accounting_finance: "text-p-af",
  operations_strategy: "text-p-os",
  system_design: "text-p-sd",
  sql: "text-p-sql",
  python: "text-p-py",
};
