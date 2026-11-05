import { memo, type ComponentPropsWithoutRef } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { tokenize } from "../../lib/highlight";

function Code({ className, children, ...rest }: ComponentPropsWithoutRef<"code">) {
  const lang = /language-(\w+)/.exec(className ?? "")?.[1];
  const text = String(children ?? "");
  if (!lang) return <code {...rest}>{children}</code>;
  return (
    <code className={className}>
      {tokenize(text.replace(/\n$/, ""), lang).map((t, i) =>
        t.kind === "plain" ? t.text : (
          <span key={i} className={`tok-${t.kind}`}>
            {t.text}
          </span>
        ),
      )}
    </code>
  );
}

const components: Components = { code: Code };

export const Markdown = memo(function Markdown({ children, className = "" }: { children: string; className?: string }) {
  return (
    <div className={`md ${className}`}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {children}
      </ReactMarkdown>
    </div>
  );
});
