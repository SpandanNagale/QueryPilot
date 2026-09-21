"use client";

import { FormEvent, useState } from "react";

export function ChatComposer({
  disabled,
  onSend,
  prefill,
}: {
  disabled: boolean;
  onSend: (message: string) => void;
  prefill?: string;
}) {
  const [value, setValue] = useState(prefill ?? "");

  function submit(e?: FormEvent) {
    e?.preventDefault();
    const trimmed = value.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed);
    setValue("");
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-2">
      <textarea
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            submit();
          }
        }}
        placeholder="Ask about the data, or ask to change something…"
        rows={3}
        disabled={disabled}
        aria-label="Message"
        className="w-full resize-none rounded-lg border border-border-1 bg-surface-1 px-3 py-2.5 text-sm text-ink-0 placeholder:text-ink-2 focus:outline-none focus:ring-1 focus:ring-teal disabled:opacity-50"
      />
      <button
        type="submit"
        disabled={disabled || !value.trim()}
        className="self-end rounded-md bg-teal px-4 py-1.5 text-sm font-medium text-surface-0 hover:opacity-90 disabled:opacity-40"
      >
        {disabled ? "Working…" : "Send"}
      </button>
    </form>
  );
}
