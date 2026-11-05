import type { SkillNode, TextbookChapter } from "../../api/types";

export function chapterFor(node: SkillNode | undefined, id: string): TextbookChapter {
  const title = node?.title ?? id;
  return {
    skill_node: id,
    title: `${title}: from first principles to this case`,
    markdown: `## Why it matters here

Halcyon's revenue is growing but its cash is not. **${title}** is the lens that separates *timing* from *trapped value*. The textbook definition only gets you halfway; the case data is dirty, so the second half of this chapter is about measuring it honestly.

### The core identity

Days sales outstanding measures how many days of revenue sit in receivables:

\`\`\`
DSO = ending AR / revenue in period × days in period
\`\`\`

A rising DSO with flat terms means customers pay slower, invoices are disputed, or someone is extending terms off-contract.

### Measuring it in SQL

\`\`\`sql
WITH clean AS (
  SELECT DISTINCT invoice_id, customer_id,
         CASE WHEN amount_cents < 10000 THEN amount_cents * 100 ELSE amount_cents END AS amount_cents,
         TRY_STRPTIME(issue_date, ['%Y-%m-%d', '%m/%d/%Y'])::DATE AS issued
  FROM invoices
  WHERE status = 'open'
)
SELECT date_trunc('quarter', issued) AS q, SUM(amount_cents) / 100.0 AS open_ar_usd
FROM clean
GROUP BY 1
ORDER BY 1;
\`\`\`

### Checking it in Python

\`\`\`python
inv = tables["invoices"].drop_duplicates("invoice_id")
inv["issued"] = pd.to_datetime(inv["issue_date"], format="mixed")
open_ar = inv.loc[inv.status == "open", "amount_cents"].sum() / 100
print(f"open AR: \${open_ar:,.0f}")
\`\`\`

### Common traps
1. Computing DSO on **gross** AR when credit memos have not been applied.
2. Counting duplicated invoice keys twice.
3. Treating dollar-denominated legacy rows as cents (a 100× understatement).
4. Using annual revenue with a quarterly AR balance.`,
    self_checks: [
      { question: "AR rises 20% while quarterly revenue rises 5%. What happens to DSO?", answer: "DSO rises by about 14% (1.20 / 1.05 − 1), because DSO scales with AR/revenue." },
      { question: "Why do duplicate invoice keys inflate DSO?", answer: "They double-count open AR in the numerator without adding revenue to the denominator." },
      { question: "Which table tells you whether long terms were contracted or granted informally?", answer: "customers.payment_terms_days (contracted) compared with due_date − issue_date on invoices and collections_notes holds." },
    ],
    snippets: [
      { language: "sql", purpose: "Deduplicate a key, keeping the latest row (DuckDB QUALIFY).", code: "SELECT *\nFROM invoices\nQUALIFY ROW_NUMBER() OVER (PARTITION BY invoice_id ORDER BY updated_at DESC) = 1;" },
      { language: "python", purpose: "Aging buckets with pandas.cut.", code: "days = (pd.Timestamp('2024-12-31') - inv.due_date).dt.days\ninv['bucket'] = pd.cut(days, [-10**6, 0, 30, 60, 90, 10**6], labels=['current', '1-30', '31-60', '61-90', '90+'])" },
    ],
    leak_redacted: false,
  };
}
