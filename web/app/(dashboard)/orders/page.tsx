"use client";

import { OrdersFeed } from "@/components/orders-feed";
import { PageHeader } from "@/components/page-header";
import { useSharedAgentState } from "@/hooks/use-shared-agent-state";

export default function OrdersPage() {
  const { snapshot } = useSharedAgentState();
  const orders = snapshot?.last_orders ?? [];
  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Orders"
        subtitle={`${orders.length} most-recent (live + paper)`}
      />
      <div className="min-h-0 flex-1 p-2">
        <OrdersFeed orders={orders} />
      </div>
    </div>
  );
}
