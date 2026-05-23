"use client";

import { useEffect, useState } from "react";
import { PageHeader } from "@/components/page-header";
import { Panel } from "@/components/panel";

interface SettingsResp {
  mode: string;
  clob_host: string;
  gamma_host: string;
  log_level: string;
  sentiment_window_sec: number;
}

const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export default function SettingsPage() {
  const [data, setData] = useState<SettingsResp | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    fetch(`${API}/api/settings`)
      .then((r) => r.json())
      .then((j) => setData(j))
      .catch((e) => setErr(String(e)));
  }, []);

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Settings"
        subtitle="active configuration · read-only"
      />
      <div className="p-2">
        <Panel title="Runtime configuration" subtitle="effective values">
          {err && (
            <div className="border-b border-terminal-red/40 bg-terminal-red/10 px-4 py-2 font-mono text-[11px] text-terminal-red">
              Failed to load: {err}
            </div>
          )}
          {!data && !err && (
            <div className="px-4 py-3 font-mono text-[11px] text-terminal-dim">loading…</div>
          )}
          {data && (
            <dl className="grid grid-cols-1 divide-y divide-terminal-border/60 font-mono text-[11px]">
              <Row label="MODE" value={data.mode} highlight={data.mode.startsWith("live")} />
              <Row label="CLOB host" value={data.clob_host} />
              <Row label="Gamma host" value={data.gamma_host} />
              <Row label="Log level" value={data.log_level} />
              <Row
                label="Sentiment window"
                value={`${data.sentiment_window_sec}s`}
              />
            </dl>
          )}
        </Panel>
        <p className="mt-3 px-1 font-mono text-[10px] text-terminal-dim">
          Edit `config/*.yaml` and redeploy to change these. Inline editing
          lands in Phase 7c.
        </p>
      </div>
    </div>
  );
}

function Row({
  label,
  value,
  highlight,
}: {
  label: string;
  value: string;
  highlight?: boolean;
}) {
  return (
    <div className="flex items-center justify-between px-4 py-2 hover:bg-terminal-surfaceAlt/40">
      <dt className="text-[10px] uppercase tracking-wider text-terminal-dim">{label}</dt>
      <dd
        className={
          "numeric text-right " +
          (highlight ? "text-terminal-amber" : "text-terminal-text")
        }
      >
        {value}
      </dd>
    </div>
  );
}
