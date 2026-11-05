import { useEffect, useState, type ReactNode } from "react";
import { CheckCircle2, Plug, Save, XCircle } from "lucide-react";
import { PROVIDERS, type LLMSettings, type Provider } from "../../api/types";
import { useSettings } from "../../store/settingsStore";
import { ErrorLine, SkeletonBlock } from "../ui/primitives";
import { PROVIDER_PRESETS } from "./providers";

type Form = Omit<LLMSettings, "api_key"> & { api_key: string };

function Field({ label, htmlFor, children, hint }: { label: string; htmlFor: string; children: ReactNode; hint?: string }) {
  return (
    <div>
      <label className="field-label" htmlFor={htmlFor}>{label}</label>
      {children}
      {hint && <p className="mt-1 text-2xs text-muted">{hint}</p>}
    </div>
  );
}

export function LlmForm() {
  const s = useSettings((x) => x.settings);
  const loading = useSettings((x) => x.loading);
  const error = useSettings((x) => x.error);
  const load = useSettings((x) => x.load);
  const save = useSettings((x) => x.save);
  const test = useSettings((x) => x.test);
  const testing = useSettings((x) => x.testing);
  const result = useSettings((x) => x.testResult);
  const saving = useSettings((x) => x.saving);
  const [form, setForm] = useState<Form | null>(null);

  useEffect(() => {
    if (!s) void load();
  }, [s, load]);

  useEffect(() => {
    if (s) setForm({ provider: s.provider, base_url: s.base_url, api_key: "", fast_model: s.fast_model, deep_model: s.deep_model, temperature_fast: s.temperature_fast, temperature_deep: s.temperature_deep, max_tokens: s.max_tokens });
  }, [s]);

  if (error && !s) return <ErrorLine message={error} onRetry={() => void load()} />;
  if (loading || !form || !s) return <SkeletonBlock lines={10} />;

  const preset = PROVIDER_PRESETS[form.provider];
  const upd = (patch: Partial<Form>) => setForm({ ...form, ...patch });
  const pickProvider = (p: Provider) => {
    const pr = PROVIDER_PRESETS[p];
    upd({ provider: p, base_url: pr.baseUrl, fast_model: pr.fast[0], deep_model: pr.deep[0] });
  };

  return (
    <form
      className="space-y-4 p-4"
      onSubmit={(e) => {
        e.preventDefault();
        void save(form);
      }}
    >
      <div>
        <span className="field-label">Provider</span>
        <div className="grid grid-cols-4 gap-1" role="radiogroup" aria-label="Provider">
          {PROVIDERS.map((p) => (
            <button
              type="button"
              key={p}
              role="radio"
              aria-checked={form.provider === p}
              onClick={() => pickProvider(p)}
              className={`h-8 rounded border text-xs ${form.provider === p ? "border-accent bg-accent/10 text-fg" : "border-line text-muted hover:text-fg"}`}
            >
              {PROVIDER_PRESETS[p].label.split(" ")[0]}
            </button>
          ))}
        </div>
      </div>
      <Field label="Base URL" htmlFor="base_url" hint={form.provider === "anthropic" ? "Native Messages API." : "OpenAI chat-completions compatible endpoint."}>
        <input id="base_url" className="input font-mono" value={form.base_url} onChange={(e) => upd({ base_url: e.target.value })} />
      </Field>
      <Field label="API key" htmlFor="api_key" hint={s.api_key_masked ? `Stored: ${s.api_key_masked}. Leave empty to keep it.` : "No key stored."}>
        <input id="api_key" type="password" autoComplete="off" className="input font-mono" placeholder={s.api_key_masked || preset.keyHint} value={form.api_key} onChange={(e) => upd({ api_key: e.target.value })} />
      </Field>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Fast model" htmlFor="fast_model" hint="Dossier, adversary, narration.">
          <input id="fast_model" list="fast-models" className="input font-mono" value={form.fast_model} onChange={(e) => upd({ fast_model: e.target.value })} />
          <datalist id="fast-models">{preset.fast.map((m) => <option key={m} value={m} />)}</datalist>
        </Field>
        <Field label="Deep model" htmlFor="deep_model" hint="Textbook synthesis, rubric grading.">
          <input id="deep_model" list="deep-models" className="input font-mono" value={form.deep_model} onChange={(e) => upd({ deep_model: e.target.value })} />
          <datalist id="deep-models">{preset.deep.map((m) => <option key={m} value={m} />)}</datalist>
        </Field>
      </div>
      <div className="flex flex-wrap gap-1">
        {[...new Set([...preset.fast, ...preset.deep])].map((m) => (
          <span key={m} className="chip">{m}</span>
        ))}
      </div>
      <div className="grid grid-cols-3 gap-3">
        <Field label={`Temp fast · ${form.temperature_fast.toFixed(2)}`} htmlFor="tf">
          <input id="tf" type="range" min={0} max={1.5} step={0.05} value={form.temperature_fast} onChange={(e) => upd({ temperature_fast: Number(e.target.value) })} className="w-full accent-[#5b8def]" />
        </Field>
        <Field label={`Temp deep · ${form.temperature_deep.toFixed(2)}`} htmlFor="td">
          <input id="td" type="range" min={0} max={1.5} step={0.05} value={form.temperature_deep} onChange={(e) => upd({ temperature_deep: Number(e.target.value) })} className="w-full accent-[#5b8def]" />
        </Field>
        <Field label="Max tokens" htmlFor="mt">
          <input id="mt" className="input font-mono" inputMode="numeric" value={form.max_tokens} onChange={(e) => upd({ max_tokens: Number(e.target.value.replace(/\D/g, "")) || 0 })} />
        </Field>
      </div>
      {form.provider === "anthropic" && (
        <p className="text-2xs text-muted">Current Claude models reject sampling parameters; the backend should omit temperature for this provider.</p>
      )}
      <div className="flex items-center gap-2 border-t border-line pt-3">
        <button type="submit" className="btn btn-primary" disabled={saving}>
          <Save size={13} aria-hidden /> {saving ? "Saving…" : "Save"}
        </button>
        <button type="button" className="btn" onClick={() => void test()} disabled={testing}>
          <Plug size={13} aria-hidden /> {testing ? "Testing…" : "Test connection"}
        </button>
        {result && (
          <span className={`flex min-w-0 items-center gap-1.5 text-xs ${result.ok ? "text-ok" : "text-bad"}`} role="status">
            {result.ok ? <CheckCircle2 size={13} aria-hidden /> : <XCircle size={13} aria-hidden />}
            <span className="truncate font-mono">{result.ok ? `${result.model} · ${result.latency_ms}ms` : result.error ?? "failed"}</span>
          </span>
        )}
      </div>
    </form>
  );
}
