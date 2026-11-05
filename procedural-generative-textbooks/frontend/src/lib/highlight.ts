/**
 * Tiny regex highlighter for SQL / Python code blocks inside markdown.
 * Produces token spans; no HTML strings, so React escapes everything.
 */
export interface Token {
  text: string;
  kind: "kw" | "str" | "num" | "com" | "fn" | "plain";
}

const SQL_KW =
  /^(select|from|where|group|by|order|having|join|left|right|inner|outer|full|on|as|with|case|when|then|else|end|and|or|not|in|is|null|distinct|limit|union|all|filter|over|partition|sum|count|avg|min|max|coalesce|cast|date|interval|desc|asc|between|like|exists)$/i;
const PY_KW =
  /^(def|return|import|from|as|for|in|if|elif|else|while|with|class|lambda|try|except|finally|raise|print|True|False|None|and|or|not|is|pass|yield)$/;

const SQL_RE = /(--[^\n]*)|('(?:[^'\\]|\\.)*')|(\b\d+(?:\.\d+)?\b)|([A-Za-z_][A-Za-z0-9_]*)|(\s+|[^\sA-Za-z0-9_']+)/g;
const PY_RE =
  /(#[^\n]*)|(f?"(?:[^"\\]|\\.)*"|f?'(?:[^'\\]|\\.)*')|(\b\d+(?:\.\d+)?\b)|([A-Za-z_][A-Za-z0-9_]*)|(\s+|[^\sA-Za-z0-9_'"#]+)/g;

export function tokenize(code: string, lang: string): Token[] {
  const isSql = /sql/i.test(lang);
  const isPy = /py/i.test(lang);
  if (!isSql && !isPy) return [{ text: code, kind: "plain" }];
  const re = new RegExp(isSql ? SQL_RE : PY_RE);
  const out: Token[] = [];
  let m: RegExpExecArray | null;
  while ((m = re.exec(code)) !== null) {
    if (m[0] === "") {
      re.lastIndex += 1;
      continue;
    }
    if (m[1]) out.push({ text: m[1], kind: "com" });
    else if (m[2]) out.push({ text: m[2], kind: "str" });
    else if (m[3]) out.push({ text: m[3], kind: "num" });
    else if (m[4]) {
      const word = m[4];
      const kw = isSql ? SQL_KW.test(word) : PY_KW.test(word);
      const fn = !kw && code[re.lastIndex] === "(";
      out.push({ text: word, kind: kw ? "kw" : fn ? "fn" : "plain" });
    } else out.push({ text: m[0], kind: "plain" });
  }
  return out;
}
