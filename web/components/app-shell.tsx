"use client";

import { useEffect, useState } from "react";
import { HamburgerButton, Sidebar } from "./sidebar";
import { HeaderBar } from "./header-bar";
import { useAgentState } from "@/hooks/use-agent-state";
import { disengageKillSwitch, engageKillSwitch } from "@/lib/api";

interface AppShellProps {
  children: React.ReactNode;
}

export function AppShell({ children }: AppShellProps) {
  const { snapshot, connected, lastUpdateMs } = useAgentState();
  const [open, setOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(false);

  const toggleKillSwitch = async () => {
    if (!snapshot) return;
    const ok = snapshot.kill_switch_engaged
      ? confirm("Disengage kill-switch? New orders will resume.")
      : confirm("Engage kill-switch? New orders will be rejected.");
    if (!ok) return;
    try {
      if (snapshot.kill_switch_engaged) {
        await disengageKillSwitch();
      } else {
        await engageKillSwitch("manual via UI");
      }
    } catch (e) {
      alert(`Kill-switch toggle failed: ${e}`);
    }
  };

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA")) return;
      if (e.key === "k" || e.key === "K") {
        e.preventDefault();
        toggleKillSwitch();
      } else if (e.key === "b" || e.key === "B") {
        e.preventDefault();
        setCollapsed((c) => !c);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [snapshot]);

  return (
    <div className="flex h-screen bg-terminal-bg text-terminal-text">
      <Sidebar
        open={open}
        collapsed={collapsed}
        onClose={() => setOpen(false)}
        onToggleCollapse={() => setCollapsed((c) => !c)}
      />

      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex items-center gap-2 border-b border-terminal-border bg-terminal-surfaceAlt px-2 py-2 lg:hidden">
          <HamburgerButton onClick={() => setOpen(true)} />
          <span className="font-mono text-[11px] uppercase tracking-[0.2em] text-terminal-amber">
            POLY • MERIDIAN
          </span>
        </div>

        <HeaderBar
          snapshot={snapshot}
          connected={connected}
          lastUpdateMs={lastUpdateMs}
          onKillSwitchToggle={toggleKillSwitch}
        />

        <main className="min-h-0 flex-1 overflow-auto">{children}</main>

        <footer className="flex items-center justify-between border-t border-terminal-border bg-terminal-surfaceAlt px-3 py-1 font-mono text-[10px] uppercase tracking-wider text-terminal-dim">
          <span className="hidden md:inline">
            [G+O]OVERVIEW [G+P]PORTFOLIO [G+M]MARKETS [G+S]STRATEGIES [G+R]ORDERS [G+W]SMART-MONEY [K]KILL
          </span>
          <span className="md:hidden">[K] kill-switch</span>
          <span>
            UPTIME{" "}
            <span className="numeric text-terminal-text">
              {formatUptime(snapshot?.uptime_sec ?? 0)}
            </span>
          </span>
        </footer>
      </div>
    </div>
  );
}

function formatUptime(s: number): string {
  if (!s) return "—";
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}d ${h}h ${m}m`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}
