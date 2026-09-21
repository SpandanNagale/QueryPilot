"use client";

import { FormEvent, useEffect, useState } from "react";
import { fetchBudgetHealth, fetchProviderHealth } from "@/lib/api";
import { ByokCredentials } from "@/lib/api";
import { BudgetHealth, ProviderHealth } from "@/lib/types";

const PROVIDER_LABELS: Record<string, string> = {
  ollama_cloud: "Ollama Cloud",
  openrouter: "OpenRouter",
  gemini: "Gemini",
  groq: "Groq",
  ollama_local: "Ollama (local)",
};

export function ProviderBudgetPanel({
  onByokChange,
}: {
  onByokChange: (byok: ByokCredentials | null) => void;
}) {
  const [providers, setProviders] = useState<ProviderHealth | null>(null);
  const [providerError, setProviderError] = useState<string | null>(null);
  const [budget, setBudget] = useState<BudgetHealth | null>(null);

  useEffect(() => {
    fetchBudgetHealth()
      .then(setBudget)
      .catch(() => setBudget(null));
  }, []);

  function refreshProviders() {
    setProviders(null);
    setProviderError(null);
    fetchProviderHealth()
      .then(setProviders)
      .catch((e) => setProviderError(e instanceof Error ? e.message : String(e)));
  }

  const budgetExceeded = budget?.exceeded ?? false;
  const [manuallyOpened, setManuallyOpened] = useState(false);
  // Derived at render time, not synced via an effect: the panel should
  // show whenever the budget is exceeded OR the user opened it themselves.
  const byokOpen = manuallyOpened || budgetExceeded;

  return (
    <div className="flex flex-col gap-3">
      <div>
        <div className="flex items-center justify-between mb-1.5">
          <h3 className="text-xs font-telemetry text-ink-2">Providers</h3>
          {/* Real, billable pings -- manual refresh only, never polled. */}
          <button onClick={refreshProviders} className="text-[11px] font-telemetry text-teal hover:underline">
            {providers ? "refresh" : "check"}
          </button>
        </div>
        {providerError && <p className="text-[11px] text-coral">{providerError}</p>}
        {providers && (
          <div className="flex flex-col gap-1">
            {Object.entries(providers).map(([alias, entries]) => (
              <div key={alias} className="flex items-center gap-1.5 flex-wrap text-[11px] font-telemetry">
                <span className="text-ink-2 w-14 shrink-0">{alias}</span>
                {entries.map((e, i) => (
                  <span
                    key={i}
                    title={e.detail}
                    className={`inline-block size-2 rounded-full ${e.status === "ok" ? "bg-teal" : "bg-coral"}`}
                  />
                ))}
                {entries.map((e) => PROVIDER_LABELS[e.provider ?? ""] ?? e.provider).join(", ")}
              </div>
            ))}
          </div>
        )}
      </div>

      {budget && (
        <div>
          <h3 className="text-xs font-telemetry text-ink-2 mb-1.5">Daily budget</h3>
          {budget.cap > 0 ? (
            <>
              <div className="h-1.5 rounded bg-surface-2 overflow-hidden">
                <div
                  className={`h-full ${budgetExceeded ? "bg-coral" : "bg-teal"}`}
                  style={{ width: `${Math.min(100, (budget.spent / budget.cap) * 100)}%` }}
                />
              </div>
              <p className="text-[11px] font-telemetry text-ink-2 mt-1">
                ${budget.spent.toFixed(4)} / ${budget.cap.toFixed(2)}
              </p>
            </>
          ) : (
            <p className="text-[11px] font-telemetry text-ink-2">No cap configured</p>
          )}
        </div>
      )}

      <div id="byok-section">
        <button
          onClick={() => setManuallyOpened((v) => !v)}
          className="text-[11px] font-telemetry text-teal hover:underline"
        >
          {byokOpen ? "Hide" : "Use your own API key"}
        </button>
        {byokOpen && <ByokForm onChange={onByokChange} />}
      </div>
    </div>
  );
}

function ByokForm({ onChange }: { onChange: (byok: ByokCredentials | null) => void }) {
  const [provider, setProvider] = useState("openrouter");
  const [apiKey, setApiKey] = useState("");
  const [active, setActive] = useState(false);

  function submit(e: FormEvent) {
    e.preventDefault();
    if (!apiKey.trim()) return;
    onChange({ provider, apiKey: apiKey.trim() });
    setActive(true);
  }

  function clear() {
    setApiKey("");
    setActive(false);
    onChange(null);
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-1.5 mt-2">
      <select
        value={provider}
        onChange={(e) => setProvider(e.target.value)}
        className="rounded-md border border-border-1 bg-surface-1 px-2 py-1 text-xs text-ink-0"
      >
        <option value="openrouter">OpenRouter</option>
        <option value="groq">Groq</option>
        <option value="gemini">Gemini</option>
        <option value="ollama_cloud">Ollama Cloud</option>
      </select>
      <input
        type="password"
        value={apiKey}
        onChange={(e) => setApiKey(e.target.value)}
        placeholder="API key"
        autoComplete="off"
        className="rounded-md border border-border-1 bg-surface-1 px-2 py-1 text-xs text-ink-0 placeholder:text-ink-2"
      />
      <div className="flex gap-2">
        <button type="submit" className="rounded-md bg-teal px-2.5 py-1 text-xs font-medium text-surface-0">
          {active ? "Update" : "Use this key"}
        </button>
        {active && (
          <button type="button" onClick={clear} className="text-xs text-ink-2 hover:text-ink-0">
            Clear
          </button>
        )}
      </div>
      {active && <p className="text-[10px] text-teal">Active for your next message.</p>}
    </form>
  );
}
