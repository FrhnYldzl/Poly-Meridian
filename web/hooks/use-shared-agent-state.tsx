"use client";

import { createContext, useContext, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { fetchState, streamUrl } from "@/lib/api";
import type { AgentSnapshot, StreamEvent } from "@/lib/types";

interface SharedState {
  snapshot: AgentSnapshot | null;
  connected: boolean;
  error: string | null;
  lastUpdateMs: number;
}

const initial: SharedState = {
  snapshot: null,
  connected: false,
  error: null,
  lastUpdateMs: 0,
};

const Ctx = createContext<SharedState>(initial);

export function AgentStateProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<SharedState>(initial);
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    let cancelled = false;

    fetchState()
      .then((snap) => {
        if (!cancelled) setState((s) => ({ ...s, snapshot: snap, lastUpdateMs: Date.now() }));
      })
      .catch((e) => {
        if (!cancelled) setState((s) => ({ ...s, error: String(e) }));
      });

    let es: EventSource | null = null;
    const t = setTimeout(() => {
      if (cancelled) return;
      es = new EventSource(streamUrl());
      esRef.current = es;
      es.onopen = () => setState((s) => ({ ...s, connected: true, error: null }));
      es.onerror = () => setState((s) => ({ ...s, connected: false }));
      es.onmessage = (msg) => {
        if (!msg.data) return;
        try {
          const evt = JSON.parse(msg.data) as StreamEvent;
          setState((s) => reduce(s, evt));
        } catch {
          /* ignore malformed */
        }
      };
    }, 1500);

    return () => {
      cancelled = true;
      clearTimeout(t);
      es?.close();
    };
  }, []);

  return <Ctx.Provider value={state}>{children}</Ctx.Provider>;
}

export function useSharedAgentState(): SharedState {
  return useContext(Ctx);
}

function reduce(s: SharedState, evt: StreamEvent): SharedState {
  const now = Date.now();
  switch (evt.type) {
    case "snapshot":
      return { ...s, snapshot: evt.data, lastUpdateMs: now };
    case "signal":
      return {
        ...s,
        lastUpdateMs: now,
        snapshot: s.snapshot && {
          ...s.snapshot,
          last_signals: [evt.data, ...s.snapshot.last_signals].slice(0, 50),
        },
      };
    case "order":
      return {
        ...s,
        lastUpdateMs: now,
        snapshot: s.snapshot && {
          ...s.snapshot,
          last_orders: [evt.data, ...s.snapshot.last_orders].slice(0, 50),
        },
      };
    case "cluster":
      return {
        ...s,
        lastUpdateMs: now,
        snapshot: s.snapshot && {
          ...s.snapshot,
          smart_money_clusters: [
            evt.data,
            ...s.snapshot.smart_money_clusters.filter(
              (c) => c.condition_id !== evt.data.condition_id,
            ),
          ].slice(0, 30),
        },
      };
    case "kill_switch":
      return {
        ...s,
        lastUpdateMs: now,
        snapshot: s.snapshot && {
          ...s.snapshot,
          kill_switch_engaged: evt.engaged,
          kill_switch_reason: evt.reason ?? null,
        },
      };
    case "heartbeat":
      return { ...s, lastUpdateMs: now };
    default:
      return s;
  }
}
