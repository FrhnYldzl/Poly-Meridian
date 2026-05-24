"use client";

import { useEffect, useState } from "react";

const STORAGE_KEY = "pm.nav.history.v1";
const SAMPLE_GAP_MS = 30_000;    // record at most once per 30s
const MAX_POINTS = 480;          // ~4 hours at 30s sampling (240) doubled

export interface NavPoint {
  ts: number;
  nav: number;
}

/** Persist a rolling NAV time-series in localStorage so the equity curve
 * survives tab switches and page reloads. Sampling is throttled to once per
 * SAMPLE_GAP_MS to keep the buffer small. */
export function useNavHistory(nav: number | undefined): NavPoint[] {
  const [points, setPoints] = useState<NavPoint[]>(() => loadInitial());

  useEffect(() => {
    if (typeof window === "undefined") return;
    if (nav == null || !Number.isFinite(nav) || nav <= 0) return;

    const now = Date.now();
    setPoints((prev) => {
      const last = prev[prev.length - 1];
      if (last && now - last.ts < SAMPLE_GAP_MS && nav === last.nav) {
        return prev;
      }
      const next = [...prev, { ts: now, nav }].slice(-MAX_POINTS);
      try {
        window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
      } catch {
        // localStorage may be unavailable (private mode); silently skip.
      }
      return next;
    });
  }, [nav]);

  return points;
}

function loadInitial(): NavPoint[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (p): p is NavPoint =>
        typeof p?.ts === "number" && typeof p?.nav === "number",
    );
  } catch {
    return [];
  }
}
