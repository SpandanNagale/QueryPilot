"use client";

import { useCallback, useReducer, useRef } from "react";
import { ByokCredentials, HttpErrorDetail, startChatStream, startConfirmStream } from "./api";
import { chatReducer, ChatState, initialChatState } from "./pipeline";
import { parseSseStream } from "./sse";

export function useChat(sessionId: string) {
  const [state, dispatch] = useReducer(chatReducer, undefined, initialChatState);
  const abortRef = useRef<AbortController | null>(null);

  const drain = useCallback(async (resPromise: Promise<Response>, controller: AbortController) => {
    try {
      const res = await resPromise;
      if (!res.ok) {
        const body = (await res.json().catch(() => null)) as { detail?: HttpErrorDetail } | null;
        const detail = body?.detail;
        dispatch({ type: "http_error", code: detail?.code ?? "INTERNAL", message: detail?.message ?? `HTTP ${res.status}` });
        return;
      }
      for await (const frame of parseSseStream(res, controller.signal)) {
        dispatch({ type: "sse", frame });
      }
    } catch (e) {
      if (controller.signal.aborted) return;
      dispatch({ type: "stream_error", message: e instanceof Error ? e.message : String(e) });
    }
  }, []);

  const sendMessage = useCallback(
    (message: string, opts: { byok?: ByokCredentials } = {}) => {
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      dispatch({ type: "user_message", message });
      void drain(startChatStream(sessionId, message, { byok: opts.byok, signal: controller.signal }), controller);
    },
    [sessionId, drain]
  );

  const confirm = useCallback(
    (confirmed: boolean) => {
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      dispatch({ type: "confirm_resume" });
      void drain(startConfirmStream(sessionId, confirmed, { signal: controller.signal }), controller);
    },
    [sessionId, drain]
  );

  const cancelStream = useCallback(() => abortRef.current?.abort(), []);

  return { state, sendMessage, confirm, cancelStream };
}

export type { ChatState };
