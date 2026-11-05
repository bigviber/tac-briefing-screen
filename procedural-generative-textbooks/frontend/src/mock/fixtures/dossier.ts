import type { Dossier } from "../../api/types";

export const DOSSIER: Dossier = {
  title: "Halcyon Metrics: growing into a cash problem",
  situation:
    "Halcyon booked its eighth straight quarter of revenue growth, yet cash fell from $14.2M to the revolver floor. The board meets in nine days and the CFO needs a diagnosis she can defend.",
  background_markdown: `## Background

Halcyon Metrics sells usage analytics to logistics and healthcare operators. Since the **Enterprise tier** launched in 2023Q3, ARR has compounded at roughly **5.8% per quarter**, and the sales team hit plan in seven of eight quarters.

Finance closed 2024Q4 last week. Three things stand out in the flash report:

| Metric | 2023Q4 | 2024Q4 |
|---|---:|---:|
| Revenue (quarter) | $10.7M | $14.3M |
| Cash on hand | $11.9M | $2.0M |
| Revolver drawn | $0.0M | $2.4M+ |

The billing platform was migrated in 2024Q2, and the controller believes "timing noise from the migration" explains most of the gap. Collections is two people who also handle vendor onboarding.

> Every number you need is in the data room. The ledger ties to the cent; the operational tables do not always tie to the ledger.

### What you have
- Nine tables exported from billing, payments, CRM and the general ledger (DuckDB, read-only)
- Quarterly statements and ratios for 2023Q1–2024Q4
- A Slack thread and an email chain from the last two weeks`,
  stakeholder_messages: [
    {
      from_name: "Dana Okafor",
      role: "CFO",
      channel: "email",
      body: "Team — I need a clean answer before the board pre-read goes out Friday. Revenue is up 33% year over year and we are drawing on the revolver. If this is migration noise, prove it. If it is not, tell me what it is, how big it is, and what we do in Q1. Please do not hand me a list of twelve possibilities.",
    },
    {
      from_name: "Marcus Hale",
      role: "VP Sales",
      channel: "slack",
      body: "fwiw enterprise customers ALWAYS pay slow in Q4, procurement freezes. we gave a couple of ramp concessions to land Orchard and Vantor but that's standard. please don't let finance start dunning my top accounts during renewal season 🙏",
    },
    {
      from_name: "Priya Raman",
      role: "Controller",
      channel: "slack",
      body: "The new billing system writes issue_date in two formats and some legacy invoices were imported in dollars, not cents. I've been adjusting AR manually at close. I'd guess 60–70% of the DSO jump is that.",
    },
    {
      from_name: "Board pre-read",
      role: "Audit committee",
      channel: "board_deck",
      body: "Q4 KPI page: ARR $47.5M (+33% YoY). NRR 108%. Cash conversion: *under review*. Covenant: minimum liquidity $2.0M — currently at threshold.",
    },
  ],
  the_ask:
    "Diagnose why cash is falling while revenue grows, quantify the cash trapped in receivables, and recommend a Q1 plan that keeps Halcyon above its liquidity covenant.",
  deliverables: [
    "Executive memo (≤ 1 page) with root cause and recommendation",
    "Four key metrics: DSO 2024Q4, AR > 90 days %, excess AR, ramp concessions",
    "Supporting SQL or Python evidence",
    "Quarter plan using the decision levers",
  ],
  data_room_index: ["customers", "plans", "subscriptions", "invoices", "invoice_lines", "payments", "credit_memos", "collections_notes", "gl_entries"],
};
