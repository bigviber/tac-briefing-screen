import { useEffect, useId, useRef, useState } from "react";
import type { Mermaid as MermaidApi } from "mermaid";

let mermaidPromise: Promise<MermaidApi> | null = null;

function loadMermaid(): Promise<MermaidApi> {
  mermaidPromise ??= import("mermaid").then(({ default: m }) => {
    m.initialize({
      startOnLoad: false,
      theme: "base",
      securityLevel: "strict",
      fontFamily: "Inter, system-ui, sans-serif",
      themeVariables: {
        darkMode: true,
        background: "#0b0e14",
        primaryColor: "#161b24",
        primaryBorderColor: "#3a4558",
        primaryTextColor: "#d7dde8",
        secondaryColor: "#11151c",
        tertiaryColor: "#11151c",
        lineColor: "#6b7a92",
        textColor: "#d7dde8",
        mainBkg: "#161b24",
        nodeBorder: "#3a4558",
        clusterBkg: "#11151c",
        edgeLabelBackground: "#11151c",
        fontSize: "12px",
        attributeBackgroundColorOdd: "#11151c",
        attributeBackgroundColorEven: "#161b24",
      },
    });
    return m;
  });
  return mermaidPromise;
}

/** Renders a mermaid diagram; shows the parse error inline instead of throwing. */
export function Mermaid({ code, className = "", natural = false }: { code: string; className?: string; natural?: boolean }) {
  const id = useId().replace(/[^a-zA-Z0-9]/g, "");
  const host = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);

  useEffect(() => {
    const mine = ++seq.current;
    const timer = window.setTimeout(async () => {
      try {
        const m = await loadMermaid();
        await m.parse(code);
        const { svg } = await m.render(`mmd${id}${mine}`, code);
        if (mine !== seq.current || !host.current) return;
        host.current.innerHTML = svg;
        const el = host.current.querySelector("svg");
        if (natural && el?.style.maxWidth) {
          // Render at intrinsic size (scrollable) instead of shrinking wide diagrams to fit.
          el.style.width = el.style.maxWidth;
          el.style.maxWidth = "none";
        }
        setError(null);
      } catch (e) {
        if (mine !== seq.current) return;
        setError(e instanceof Error ? e.message : String(e));
        document.getElementById(`dmmd${id}${mine}`)?.remove();
      }
    }, 180);
    return () => window.clearTimeout(timer);
  }, [code, id, natural]);

  return (
    <div className={className}>
      {error && (
        <pre className="mb-2 whitespace-pre-wrap rounded border border-bad/40 bg-bad/10 p-2 font-mono text-2xs text-[#f3a0aa]">{error}</pre>
      )}
      <div ref={host} className={`mermaid-host ${natural ? "w-max min-w-full" : "flex justify-center"}`} />
    </div>
  );
}
