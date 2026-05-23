"use client";

import { ComingSoon, PageHeader } from "@/components/page-header";

export default function BacktestPage() {
  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Backtest"
        subtitle="historical replay · walk-forward folds · live-gate verdict"
        badge="Phase 7b"
      />
      <div className="flex-1 p-2">
        <ComingSoon phase="Phase 7b" />
      </div>
    </div>
  );
}
