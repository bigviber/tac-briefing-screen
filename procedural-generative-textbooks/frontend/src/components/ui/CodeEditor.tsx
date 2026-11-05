import { useEffect, useRef } from "react";
import Editor, { loader, type OnMount } from "@monaco-editor/react";
import * as monaco from "monaco-editor";
import EditorWorker from "monaco-editor/esm/vs/editor/editor.worker?worker";

// Bundle Monaco locally (no CDN) so the workbench works offline.
self.MonacoEnvironment = { getWorker: () => new EditorWorker() };
loader.config({ monaco });

monaco.editor.defineTheme("axiom-dark", {
  base: "vs-dark",
  inherit: true,
  rules: [
    { token: "keyword", foreground: "b78cff" },
    { token: "string", foreground: "9ece6a" },
    { token: "number", foreground: "ff9e64" },
    { token: "comment", foreground: "5c6779", fontStyle: "italic" },
    { token: "operator", foreground: "8b95a7" },
  ],
  colors: {
    "editor.background": "#0b0e14",
    "editor.foreground": "#d7dde8",
    "editorLineNumber.foreground": "#3a4456",
    "editorLineNumber.activeForeground": "#8b95a7",
    "editor.lineHighlightBackground": "#11151c",
    "editor.selectionBackground": "#2a3a5c",
    "editorCursor.foreground": "#5b8def",
    "editorIndentGuide.background1": "#1a2029",
    "editorWidget.background": "#161b24",
    "editorWidget.border": "#232a36",
    "scrollbarSlider.background": "#2a324180",
  },
});

interface Props {
  value: string;
  language: string;
  onChange?: (v: string) => void;
  /** Called on ⌘/Ctrl+Enter. */
  onRun?: () => void;
  readOnly?: boolean;
  ariaLabel: string;
}

export function CodeEditor({ value, language, onChange, onRun, readOnly = false, ariaLabel }: Props) {
  const runRef = useRef(onRun);
  useEffect(() => {
    runRef.current = onRun;
  }, [onRun]);

  const onMount: OnMount = (editor, m) => {
    editor.addCommand(m.KeyMod.CtrlCmd | m.KeyCode.Enter, () => runRef.current?.());
  };

  return (
    <Editor
      value={value}
      language={language}
      theme="axiom-dark"
      onChange={(v) => onChange?.(v ?? "")}
      onMount={onMount}
      loading={<div className="p-3 font-mono text-xs text-muted">Loading editor…</div>}
      options={{
        readOnly,
        ariaLabel,
        fontFamily: "'JetBrains Mono', ui-monospace, monospace",
        fontSize: 12,
        lineHeight: 18,
        minimap: { enabled: false },
        scrollBeyondLastLine: false,
        renderLineHighlight: "line",
        padding: { top: 8, bottom: 8 },
        lineNumbersMinChars: 3,
        folding: false,
        wordWrap: language === "markdown" ? "on" : "off",
        automaticLayout: true,
        tabSize: language === "python" ? 4 : 2,
        overviewRulerLanes: 0,
        scrollbar: { verticalScrollbarSize: 8, horizontalScrollbarSize: 8 },
      }}
    />
  );
}
