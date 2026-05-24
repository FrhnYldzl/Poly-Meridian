"use client";

import { Panel } from "./panel";
import { NavSpark } from "./nav-spark";
import { formatUsd, relativeTime } from "@/lib/utils";
import type { AgentSnapshot } from "@/lib/types";

interface ActivityPanelProps {
  snapshot: AgentSnapshot | null;
}

export function ActivityPanel({ snapshot }: ActivityPanelProps) {
  const ticks = snapshot?.pipeline_ticks_total ?? 0;
  const evals = snapshot?.strategies_evaluated_total ?? 0;
  const arbSeen = snapshot?.arb_opportunities_total ?? 0;
  const uptime = snapshot?.uptime_sec ?? 0;
  const ticksPerMin = uptime > 0 ? (ticks / uptime) * 60 : 0;

  return (
    <Panel
      title="Pipeline activity"
      subtitle="autonomous · last book update"
      bodyClassName="font-mono text-[11px]"
    >
      <div className="flex flex-col gap-3 p-3">
        <NavSpark nav={snapshot?.nav_usd ?? 0} height={60} />
        <div className="text-[10px] uppercase tracking-wider text-terminal-dim">
          NAV trace · <span className="text-terminal-text">{formatUsd(snapshot?.nav_usd ?? 0)}</span>
        </div>

        <dl className="grid grid-cols-2 gap-2 border-t border-terminal-border pt-3">
          <Cell label="Ticks total" value={ticks.toLocaleString()} />
          <Cell label="Ticks / min" value={ticksPerMin.toFixed(1)} />
          <Cell label="Evaluations" value={evals.toLocaleString()} />
          <Cell label="Arb seen" value={arbSeen.toLocaleString()} highlight={arbSeen > 0} />
        </dl>

        <div className="border-t border-terminal-border pt-3 text-[10px] uppercase tracking-wider text-terminal-dim">
          Last book update:{" "}
          <span className="text-terminal-text">
            {snapshot?.last_book_update_ts
              ? relativeTime(snapshot.last_book_update_ts)
              : "—"}
          </span>
        </div>
      </div>
    </Panel>
  );
}

function Cell({
  label,
  value,
  highlight,
}: {
  label: string;
  value: string;
  highlight?: boolean;
}) {
  return (
    <div>
      <dt className="text-[10px] uppercase tracking-wider text-terminal-dim">{label}</dt>
      <dd
        className={
          "numeric mt-0.5 text-base font-semibold " +
          (highlight ? "text-terminal-amber" : "text-terminal-textBright")
        }
      >
        {value}
      </dd>
    </div>
  );
}
