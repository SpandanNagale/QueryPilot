"use client";

import { ERROR_CODE_UI } from "@/lib/nodeStateStyle";
import { ErrorEventPayload } from "@/lib/types";

export function ErrorBanner({
  error,
  onRetry,
  onResetSandbox,
  onShowByok,
}: {
  error: ErrorEventPayload;
  onRetry?: () => void;
  onResetSandbox?: () => void;
  onShowByok?: () => void;
}) {
  const ui = ERROR_CODE_UI[error.code] ?? { title: error.message, action: "Try again." };
  return (
    <div className="rounded-lg border border-coral bg-coral-dim/40 px-4 py-3 text-sm" role="alert">
      <p className="text-coral font-medium">{ui.title}</p>
      <p className="text-ink-1 text-xs mt-1">{error.message}</p>
      <div className="flex flex-wrap gap-2 mt-2">
        {error.code === "SESSION_INVALID" && onResetSandbox && (
          <button onClick={onResetSandbox} className="text-xs font-telemetry text-teal underline underline-offset-2">
            Reset sandbox
          </button>
        )}
        {error.code === "BUDGET_EXCEEDED" && onShowByok && (
          <button onClick={onShowByok} className="text-xs font-telemetry text-teal underline underline-offset-2">
            Add your own API key
          </button>
        )}
        {onRetry && !["SESSION_INVALID", "BUDGET_EXCEEDED"].includes(error.code) && (
          <button onClick={onRetry} className="text-xs font-telemetry text-teal underline underline-offset-2">
            {ui.action}
          </button>
        )}
      </div>
    </div>
  );
}
