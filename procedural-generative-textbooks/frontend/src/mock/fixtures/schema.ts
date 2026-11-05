import type { ColumnSpec, DatabaseSpec, SampleRow, TableSpec } from "../../api/types";

type Col = [name: string, type: string, nullable: boolean, description: string];

function cols(list: Col[]): ColumnSpec[] {
  return list.map(([name, type, nullable, description]) => ({ name, type, nullable, description }));
}

function ddl(name: string, columns: ColumnSpec[], pk: string[]): string {
  const body = columns.map((c) => `  ${c.name} ${c.type}${c.nullable ? "" : " NOT NULL"}`).join(",\n");
  return `CREATE TABLE ${name} (\n${body},\n  PRIMARY KEY (${pk.join(", ")})\n);`;
}

function table(
  name: string,
  rowCount: number,
  columnList: Col[],
  pk: string[],
  fks: [string, string, string][],
  dirt: string[],
  sample: SampleRow[],
): TableSpec {
  const columns = cols(columnList);
  return { name, ddl: ddl(name, columns, pk), columns, row_count: rowCount, primary_key: pk, foreign_keys: fks, dirt, sample_rows: sample };
}

export const TABLES: TableSpec[] = [
  table("customers", 1184, [
    ["customer_id", "VARCHAR", false, "Stable customer key (C-xxxxx)"],
    ["name", "VARCHAR", false, "Legal entity name"],
    ["segment", "VARCHAR", false, "smb | mid_market | enterprise"],
    ["region", "VARCHAR", true, "Billing region"],
    ["payment_terms_days", "INTEGER", false, "Contracted net terms"],
    ["created_at", "TIMESTAMP", false, "Account creation timestamp"],
  ], ["customer_id"], [], ["case_mixed:segment", "whitespace:name", "nulls:region:0.04"], [
    { customer_id: "C-10001", name: "Brightline Freight LLC", segment: "mid_market", region: "NA-East", payment_terms_days: 30, created_at: "2022-03-14 09:12:00" },
    { customer_id: "C-10002", name: "  Orchard Health Partners", segment: "Enterprise", region: "NA-West", payment_terms_days: 60, created_at: "2022-03-19 14:40:00" },
    { customer_id: "C-10003", name: "Kestrel Analytics", segment: "smb", region: null, payment_terms_days: 15, created_at: "2022-04-02 11:05:00" },
    { customer_id: "C-10004", name: "Vantor Logistics GmbH", segment: "enterprise", region: "EMEA", payment_terms_days: 90, created_at: "2022-04-11 08:30:00" },
    { customer_id: "C-10005", name: "Pinecrest Dental Group", segment: "SMB", region: "NA-East", payment_terms_days: 30, created_at: "2022-05-01 16:22:00" },
  ]),
  table("plans", 9, [
    ["plan_id", "VARCHAR", false, "Plan SKU"],
    ["name", "VARCHAR", false, "Display name"],
    ["list_price_monthly_cents", "BIGINT", false, "List price per seat, cents"],
    ["billing_interval", "VARCHAR", false, "monthly | annual"],
  ], ["plan_id"], [], [], [
    { plan_id: "P-STARTER-M", name: "Starter", list_price_monthly_cents: 4900, billing_interval: "monthly" },
    { plan_id: "P-GROWTH-M", name: "Growth", list_price_monthly_cents: 14900, billing_interval: "monthly" },
    { plan_id: "P-GROWTH-A", name: "Growth (annual)", list_price_monthly_cents: 12900, billing_interval: "annual" },
    { plan_id: "P-SCALE-A", name: "Scale (annual)", list_price_monthly_cents: 39900, billing_interval: "annual" },
    { plan_id: "P-ENT-A", name: "Enterprise", list_price_monthly_cents: 129900, billing_interval: "annual" },
  ]),
  table("subscriptions", 1631, [
    ["subscription_id", "VARCHAR", false, "Subscription key"],
    ["customer_id", "VARCHAR", false, "FK customers"],
    ["plan_id", "VARCHAR", false, "FK plans"],
    ["seats", "INTEGER", false, "Licensed seats"],
    ["start_date", "DATE", false, "Service start"],
    ["end_date", "DATE", true, "Cancellation date"],
    ["discount_pct", "DOUBLE", true, "Negotiated discount"],
  ], ["subscription_id"], [["customer_id", "customers", "customer_id"], ["plan_id", "plans", "plan_id"]], ["nulls:discount_pct:0.31"], [
    { subscription_id: "S-200001", customer_id: "C-10001", plan_id: "P-GROWTH-A", seats: 42, start_date: "2022-04-01", end_date: null, discount_pct: 0.1 },
    { subscription_id: "S-200002", customer_id: "C-10002", plan_id: "P-ENT-A", seats: 310, start_date: "2022-04-01", end_date: null, discount_pct: 0.22 },
    { subscription_id: "S-200003", customer_id: "C-10003", plan_id: "P-STARTER-M", seats: 6, start_date: "2022-04-15", end_date: "2023-02-14", discount_pct: null },
    { subscription_id: "S-200004", customer_id: "C-10004", plan_id: "P-SCALE-A", seats: 120, start_date: "2022-05-01", end_date: null, discount_pct: 0.15 },
    { subscription_id: "S-200005", customer_id: "C-10005", plan_id: "P-STARTER-M", seats: 11, start_date: "2022-05-01", end_date: null, discount_pct: null },
  ]),
  table("invoices", 14822, [
    ["invoice_id", "VARCHAR", false, "Invoice number"],
    ["customer_id", "VARCHAR", false, "FK customers"],
    ["subscription_id", "VARCHAR", true, "FK subscriptions"],
    ["issue_date", "VARCHAR", false, "Issue date (mixed formats)"],
    ["due_date", "DATE", false, "Due date per terms"],
    ["amount_cents", "BIGINT", false, "Invoice total, cents (some rows in dollars)"],
    ["status", "VARCHAR", false, "open | paid | void | written_off"],
    ["currency", "VARCHAR", false, "ISO currency"],
  ], ["invoice_id"], [["customer_id", "customers", "customer_id"], ["subscription_id", "subscriptions", "subscription_id"]], ["format_mixed:issue_date", "dup_keys:0.004", "unit_mixed:amount_cents", "orphan_fk:subscription_id:0.01"], [
    { invoice_id: "INV-0040121", customer_id: "C-10002", subscription_id: "S-200002", issue_date: "2024-10-01", due_date: "2024-11-30", amount_cents: 31480200, status: "open", currency: "USD" },
    { invoice_id: "INV-0040122", customer_id: "C-10004", subscription_id: "S-200004", issue_date: "10/01/2024", due_date: "2024-12-30", amount_cents: 4069800, status: "open", currency: "USD" },
    { invoice_id: "INV-0040123", customer_id: "C-10001", subscription_id: "S-200001", issue_date: "2024-10-01", due_date: "2024-10-31", amount_cents: 650160, status: "paid", currency: "USD" },
    { invoice_id: "INV-0040124", customer_id: "C-10005", subscription_id: "S-200005", issue_date: "2024-10-02", due_date: "2024-11-01", amount_cents: 539, status: "paid", currency: "USD" },
    { invoice_id: "INV-0040124", customer_id: "C-10005", subscription_id: "S-200005", issue_date: "2024-10-02", due_date: "2024-11-01", amount_cents: 539, status: "paid", currency: "USD" },
  ]),
  table("invoice_lines", 38210, [
    ["line_id", "VARCHAR", false, "Line key"],
    ["invoice_id", "VARCHAR", false, "FK invoices"],
    ["description", "VARCHAR", false, "Line description"],
    ["quantity", "INTEGER", false, "Units"],
    ["unit_price_cents", "BIGINT", false, "Unit price, cents"],
  ], ["line_id"], [["invoice_id", "invoices", "invoice_id"]], [], [
    { line_id: "L-1", invoice_id: "INV-0040121", description: "Enterprise seats Q4", quantity: 310, unit_price_cents: 101550 },
    { line_id: "L-2", invoice_id: "INV-0040122", description: "Scale seats Q4", quantity: 120, unit_price_cents: 33915 },
    { line_id: "L-3", invoice_id: "INV-0040123", description: "Growth seats Oct", quantity: 42, unit_price_cents: 15480 },
    { line_id: "L-4", invoice_id: "INV-0040124", description: "Starter seats Oct", quantity: 11, unit_price_cents: 4900 },
    { line_id: "L-5", invoice_id: "INV-0040124", description: "Proration credit", quantity: 1, unit_price_cents: -509 },
  ]),
  table("payments", 13107, [
    ["payment_id", "VARCHAR", false, "Processor payment id"],
    ["invoice_id", "VARCHAR", true, "FK invoices (null when unapplied)"],
    ["received_at", "TIMESTAMP", false, "Settlement time (mixed TZ)"],
    ["amount_cents", "BIGINT", false, "Cash received, cents"],
    ["method", "VARCHAR", false, "card | ach | wire"],
  ], ["payment_id"], [["invoice_id", "invoices", "invoice_id"]], ["tz_mixed:received_at", "nulls:invoice_id:0.02"], [
    { payment_id: "PAY-881201", invoice_id: "INV-0040123", received_at: "2024-10-29 17:02:11+00:00", amount_cents: 650160, method: "ach" },
    { payment_id: "PAY-881202", invoice_id: "INV-0040124", received_at: "2024-10-02 09:14:40-05:00", amount_cents: 53900, method: "card" },
    { payment_id: "PAY-881203", invoice_id: null, received_at: "2024-10-03 12:00:00+00:00", amount_cents: 1250000, method: "wire" },
    { payment_id: "PAY-881204", invoice_id: "INV-0039877", received_at: "2024-10-04 10:31:09+00:00", amount_cents: 4069800, method: "wire" },
    { payment_id: "PAY-881205", invoice_id: "INV-0039901", received_at: "2024-10-04 15:45:00-04:00", amount_cents: 199500, method: "card" },
  ]),
  table("credit_memos", 612, [
    ["memo_id", "VARCHAR", false, "Credit memo id"],
    ["invoice_id", "VARCHAR", false, "FK invoices"],
    ["issued_at", "DATE", false, "Issue date"],
    ["amount_cents", "BIGINT", false, "Credit amount, cents"],
    ["reason", "VARCHAR", true, "Free-text reason"],
  ], ["memo_id"], [["invoice_id", "invoices", "invoice_id"]], ["late_arriving:issued_at"], [
    { memo_id: "CM-3001", invoice_id: "INV-0038112", issued_at: "2024-09-28", amount_cents: 120000, reason: "SLA credit" },
    { memo_id: "CM-3002", invoice_id: "INV-0038440", issued_at: "2024-10-03", amount_cents: 2400000, reason: "Ramp concession" },
    { memo_id: "CM-3003", invoice_id: "INV-0039002", issued_at: "2024-10-09", amount_cents: 45000, reason: null },
    { memo_id: "CM-3004", invoice_id: "INV-0039120", issued_at: "2024-10-12", amount_cents: 980000, reason: "Ramp concession" },
    { memo_id: "CM-3005", invoice_id: "INV-0039544", issued_at: "2024-10-15", amount_cents: 15000, reason: "Duplicate charge" },
  ]),
  table("collections_notes", 2290, [
    ["note_id", "VARCHAR", false, "Note id"],
    ["customer_id", "VARCHAR", false, "FK customers"],
    ["created_at", "TIMESTAMP", false, "When the note was logged"],
    ["stage", "VARCHAR", false, "reminder | call | escalation | hold"],
    ["note", "VARCHAR", true, "Collector free text"],
  ], ["note_id"], [["customer_id", "customers", "customer_id"]], ["orphan_fk:customer_id:0.006"], [
    { note_id: "N-1", customer_id: "C-10002", created_at: "2024-11-05 10:00:00", stage: "reminder", note: "AP says invoice routed to procurement" },
    { note_id: "N-2", customer_id: "C-10004", created_at: "2024-11-12 15:20:00", stage: "call", note: "Waiting on PO renewal" },
    { note_id: "N-3", customer_id: "C-10002", created_at: "2024-12-02 09:45:00", stage: "escalation", note: "Sales asked to pause dunning" },
    { note_id: "N-4", customer_id: "C-10007", created_at: "2024-12-04 13:10:00", stage: "hold", note: "Dunning paused per VP Sales" },
    { note_id: "N-5", customer_id: "C-10011", created_at: "2024-12-09 11:30:00", stage: "reminder", note: null },
  ]),
  table("gl_entries", 52480, [
    ["entry_id", "VARCHAR", false, "Journal entry id"],
    ["line_no", "INTEGER", false, "Line within entry"],
    ["posted_date", "DATE", false, "Posting date"],
    ["period", "VARCHAR", false, "Fiscal quarter, e.g. 2024Q4"],
    ["account_code", "VARCHAR", false, "Chart-of-accounts code"],
    ["debit_cents", "BIGINT", false, "Debit amount"],
    ["credit_cents", "BIGINT", false, "Credit amount"],
    ["source", "VARCHAR", false, "sales | cash | adjustment | ..."],
  ], ["entry_id", "line_no"], [], [], [
    { entry_id: "JE-104221", line_no: 1, posted_date: "2024-10-01", period: "2024Q4", account_code: "1100", debit_cents: 31480200, credit_cents: 0, source: "sales" },
    { entry_id: "JE-104221", line_no: 2, posted_date: "2024-10-01", period: "2024Q4", account_code: "2200", debit_cents: 0, credit_cents: 31480200, source: "sales" },
    { entry_id: "JE-104222", line_no: 1, posted_date: "2024-10-04", period: "2024Q4", account_code: "1000", debit_cents: 4069800, credit_cents: 0, source: "cash" },
    { entry_id: "JE-104222", line_no: 2, posted_date: "2024-10-04", period: "2024Q4", account_code: "1100", debit_cents: 0, credit_cents: 4069800, source: "cash" },
    { entry_id: "JE-104230", line_no: 1, posted_date: "2024-10-03", period: "2024Q4", account_code: "4110", debit_cents: 2400000, credit_cents: 0, source: "adjustment" },
  ]),
];

export const ERD = `erDiagram
  customers ||--o{ subscriptions : has
  plans ||--o{ subscriptions : priced_by
  customers ||--o{ invoices : billed
  subscriptions ||--o{ invoices : generates
  invoices ||--|{ invoice_lines : contains
  invoices ||--o{ payments : settled_by
  invoices ||--o{ credit_memos : credited_by
  customers ||--o{ collections_notes : chased_in
  customers {
    VARCHAR customer_id PK
    VARCHAR segment
    INTEGER payment_terms_days
  }
  invoices {
    VARCHAR invoice_id PK
    VARCHAR customer_id FK
    BIGINT amount_cents
    VARCHAR status
  }
  payments {
    VARCHAR payment_id PK
    VARCHAR invoice_id FK
    TIMESTAMP received_at
  }`;

export const DATABASE: DatabaseSpec = {
  engine: "duckdb",
  path: "cases/cs_7f3a91/company.duckdb",
  tables: TABLES,
  erd_mermaid: ERD,
  total_rows: TABLES.reduce((s, t) => s + t.row_count, 0),
};
