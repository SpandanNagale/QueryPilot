// Small regex-based SQL tokenizer for read-only display -- a full syntax
// highlighting library is overkill for one language, always-read-only,
// never-edited SQL strings a few lines long.
import { Fragment } from "react";

const KEYWORDS = new Set([
  "SELECT", "FROM", "WHERE", "AND", "OR", "NOT", "IN", "IS", "NULL", "JOIN", "INNER", "LEFT", "RIGHT",
  "ON", "GROUP", "BY", "ORDER", "LIMIT", "OFFSET", "AS", "INSERT", "INTO", "VALUES", "UPDATE", "SET",
  "DELETE", "COUNT", "SUM", "AVG", "MIN", "MAX", "DISTINCT", "HAVING", "LIKE", "BETWEEN", "ASC", "DESC",
]);

export function SqlHighlight({ sql }: { sql: string }) {
  // A fresh RegExp per render, not a shared module-level one -- a `g` regex
  // is stateful (lastIndex), and mutating shared module state from inside a
  // component body isn't safe under React's stricter render-purity rules.
  const tokenRe = /('(?:[^']|'')*')|(-?\b\d+(?:\.\d+)?\b)|([A-Za-z_][A-Za-z0-9_]*)|(\s+)|(.)/g;
  const tokens: { text: string; cls: string }[] = [];
  let match: RegExpExecArray | null;
  while ((match = tokenRe.exec(sql)) !== null) {
    const [, str, num, word, space, other] = match;
    if (str !== undefined) tokens.push({ text: str, cls: "text-amber" });
    else if (num !== undefined) tokens.push({ text: num, cls: "text-coral" });
    else if (word !== undefined) {
      tokens.push({ text: word, cls: KEYWORDS.has(word.toUpperCase()) ? "text-teal font-semibold" : "text-ink-0" });
    } else if (space !== undefined) tokens.push({ text: space, cls: "" });
    else if (other !== undefined) tokens.push({ text: other, cls: "text-ink-1" });
  }
  return (
    <code className="font-telemetry text-xs leading-relaxed whitespace-pre-wrap break-words">
      {tokens.map((t, i) => (
        <Fragment key={i}>{t.cls ? <span className={t.cls}>{t.text}</span> : t.text}</Fragment>
      ))}
    </code>
  );
}
