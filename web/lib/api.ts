import type { AgentSnapshot } from "./types";

const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export async function fetchState(): Promise<AgentSnapshot> {
  const res = await fetch(`${API}/api/state`, { cache: "no-store" });
  if (!res.ok) throw new Error(`state fetch failed: ${res.status}`);
  return res.json();
}

export async function engageKillSwitch(reason: string): Promise<void> {
  const res = await fetch(
    `${API}/api/kill-switch/engage?reason=${encodeURIComponent(reason)}`,
    { method: "POST" },
  );
  if (!res.ok) throw new Error(`engage failed: ${res.status}`);
}

export async function disengageKillSwitch(): Promise<void> {
  const res = await fetch(`${API}/api/kill-switch/disengage`, { method: "POST" });
  if (!res.ok) throw new Error(`disengage failed: ${res.status}`);
}

export function streamUrl(): string {
  return `${API}/api/stream`;
}
