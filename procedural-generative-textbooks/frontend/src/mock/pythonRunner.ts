import type { PythonRequest, PythonResult } from "../api/types";

/** Render a small bar chart to a base64 PNG, as matplotlib would return it. */
function fakeFigure(): string {
  if (typeof document === "undefined") return "";
  const c = document.createElement("canvas");
  c.width = 560;
  c.height = 260;
  const g = c.getContext("2d");
  if (!g) return "";
  g.fillStyle = "#ffffff";
  g.fillRect(0, 0, c.width, c.height);
  g.fillStyle = "#222";
  g.font = "13px sans-serif";
  g.fillText("DSO by quarter (days)", 16, 22);
  const vals = [44, 46, 49, 53, 58, 64, 71, 78];
  const labels = ["23Q1", "23Q2", "23Q3", "23Q4", "24Q1", "24Q2", "24Q3", "24Q4"];
  vals.forEach((v, i) => {
    const h = (v / 90) * 190;
    const x = 40 + i * 62;
    g.fillStyle = i >= 4 ? "#d9534f" : "#4a78c2";
    g.fillRect(x, 230 - h, 40, h);
    g.fillStyle = "#333";
    g.font = "11px sans-serif";
    g.fillText(labels[i], x + 4, 248);
    g.fillText(String(v), x + 10, 226 - h);
  });
  g.strokeStyle = "#999";
  g.beginPath();
  g.moveTo(32, 230);
  g.lineTo(540, 230);
  g.stroke();
  return c.toDataURL("image/png").replace(/^data:image\/png;base64,/, "");
}

export function runPython(req: PythonRequest): PythonResult {
  const code = req.code;
  if (/raise\s+|1\s*\/\s*0/.test(code)) {
    return { stdout: "", stderr: 'Traceback (most recent call last):\n  File "<case>", line 3, in <module>\nZeroDivisionError: division by zero', figures: [], elapsed_ms: 211, error: "ZeroDivisionError: division by zero" };
  }
  const wantsPlot = /plt\.|\.plot\(/.test(code);
  return {
    stdout: [
      "tables loaded: customers, plans, subscriptions, invoices, invoice_lines, payments, credit_memos, collections_notes, gl_entries",
      "invoices: 14,822 rows -> 14,761 after dedup (61 duplicate invoice_id)",
      "legacy dollar rows rescaled: 214",
      "",
      "   period  open_ar_usd  revenue_usd  dso_days",
      "0  2024Q1    7,293,104   11,276,915      58.9",
      "1  2024Q2    8,527,701   11,930,976      65.0",
      "2  2024Q3    9,960,388   12,622,973      71.8",
      "3  2024Q4   12,204,551   13,355,105      83.2",
    ].join("\n"),
    stderr: /import warnings|warn/.test(code) ? "" : "UserWarning: Could not infer format, so each element will be parsed individually.",
    result_json: { dso_2024q4: 77.6, open_ar_usd: 12204551, dup_invoice_ids: 61, legacy_rows: 214 },
    figures: wantsPlot ? [fakeFigure()] : [],
    elapsed_ms: 640 + (code.length % 300),
  };
}
