"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Activity,
  BarChart3,
  Brain,
  ChevronLeft,
  LayoutGrid,
  LineChart,
  ListOrdered,
  Menu,
  PieChart,
  Shield,
  Sliders,
  Wallet,
  X,
} from "lucide-react";
import { cn } from "@/lib/utils";

interface NavItem {
  href: string;
  label: string;
  hotkey?: string;
  icon: React.ComponentType<{ className?: string }>;
}

const NAV_ITEMS: NavItem[] = [
  { href: "/", label: "Overview", hotkey: "G O", icon: LayoutGrid },
  { href: "/portfolio", label: "Portfolio", hotkey: "G P", icon: PieChart },
  { href: "/markets", label: "Markets", hotkey: "G M", icon: BarChart3 },
  { href: "/strategies", label: "Strategies", hotkey: "G S", icon: Brain },
  { href: "/orders", label: "Orders", hotkey: "G R", icon: ListOrdered },
  { href: "/smart-money", label: "Smart Money", hotkey: "G W", icon: Wallet },
  { href: "/backtest", label: "Backtest", hotkey: "G B", icon: LineChart },
  { href: "/risk", label: "Risk", hotkey: "G K", icon: Shield },
  { href: "/logs", label: "Logs", hotkey: "G L", icon: Activity },
  { href: "/settings", label: "Settings", hotkey: "G T", icon: Sliders },
];

interface SidebarBodyProps {
  collapsed: boolean;
  onItemClick?: () => void;
  onToggleCollapse?: () => void;
  showCloseButton?: boolean;
  onClose?: () => void;
}

function SidebarBody({
  collapsed,
  onItemClick,
  onToggleCollapse,
  showCloseButton,
  onClose,
}: SidebarBodyProps) {
  const pathname = usePathname();
  return (
    <div className="flex h-full flex-col">
      <div className="flex h-12 items-center justify-between border-b border-terminal-border px-3">
        {!collapsed ? (
          <span className="font-mono text-[11px] font-bold uppercase tracking-[0.2em] text-terminal-amber">
            POLY • MERIDIAN
          </span>
        ) : (
          <span className="font-mono text-[14px] font-bold text-terminal-amber">PM</span>
        )}
        {showCloseButton && (
          <button
            type="button"
            onClick={onClose}
            className="rounded p-1 text-terminal-dim hover:bg-terminal-surfaceAlt hover:text-terminal-text"
            aria-label="Close menu"
          >
            <X className="h-4 w-4" />
          </button>
        )}
      </div>

      <nav className="flex-1 overflow-y-auto py-2">
        <ul className="flex flex-col gap-0.5 px-2">
          {NAV_ITEMS.map((item) => {
            const active =
              item.href === "/"
                ? pathname === "/"
                : pathname.startsWith(item.href);
            return (
              <li key={item.href}>
                <Link
                  href={item.href}
                  onClick={onItemClick}
                  className={cn(
                    "group flex items-center gap-3 rounded px-2.5 py-1.5 font-mono text-[12px] uppercase tracking-wider transition-colors",
                    active
                      ? "bg-terminal-amber/15 text-terminal-amber"
                      : "text-terminal-dim hover:bg-terminal-surfaceAlt hover:text-terminal-text",
                  )}
                  title={collapsed ? item.label : undefined}
                >
                  <item.icon
                    className={cn(
                      "h-4 w-4 shrink-0",
                      active ? "text-terminal-amber" : "text-terminal-dim group-hover:text-terminal-text",
                    )}
                  />
                  {!collapsed && (
                    <>
                      <span className="flex-1 truncate">{item.label}</span>
                      {item.hotkey && (
                        <span className="font-mono text-[9px] text-terminal-dim">
                          {item.hotkey}
                        </span>
                      )}
                    </>
                  )}
                </Link>
              </li>
            );
          })}
        </ul>
      </nav>

      {onToggleCollapse && (
        <div className="border-t border-terminal-border p-2">
          <button
            type="button"
            onClick={onToggleCollapse}
            className="flex w-full items-center justify-center rounded border border-terminal-border bg-terminal-surfaceAlt px-2 py-1 font-mono text-[10px] uppercase tracking-wider text-terminal-dim hover:border-terminal-amber/40 hover:text-terminal-amber"
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          >
            <ChevronLeft
              className={cn("h-3 w-3 transition-transform", collapsed ? "rotate-180" : "")}
            />
            {!collapsed && <span className="ml-1.5">Collapse</span>}
          </button>
        </div>
      )}
    </div>
  );
}

interface SidebarProps {
  open: boolean;
  collapsed: boolean;
  onClose: () => void;
  onToggleCollapse: () => void;
}

export function Sidebar({ open, collapsed, onClose, onToggleCollapse }: SidebarProps) {
  return (
    <>
      {/* Desktop sidebar — always visible, part of flex layout. */}
      <aside
        className={cn(
          "hidden h-screen shrink-0 border-r border-terminal-border bg-terminal-surface transition-[width] duration-200 lg:block",
          collapsed ? "w-14" : "w-56",
        )}
      >
        <SidebarBody collapsed={collapsed} onToggleCollapse={onToggleCollapse} />
      </aside>

      {/* Mobile sidebar overlay */}
      <div
        className={cn(
          "fixed inset-0 z-40 bg-terminal-bg/80 backdrop-blur-sm transition-opacity lg:hidden",
          open ? "opacity-100" : "pointer-events-none opacity-0",
        )}
        onClick={onClose}
        aria-hidden="true"
      />
      <aside
        className={cn(
          "fixed top-0 bottom-0 left-0 z-50 w-56 border-r border-terminal-border bg-terminal-surface transition-transform duration-200 lg:hidden",
          open ? "translate-x-0" : "-translate-x-full",
        )}
      >
        <SidebarBody collapsed={false} onItemClick={onClose} showCloseButton onClose={onClose} />
      </aside>
    </>
  );
}

export function HamburgerButton({
  onClick,
  className,
}: {
  onClick: () => void;
  className?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "flex h-8 w-8 items-center justify-center rounded border border-terminal-border bg-terminal-bg text-terminal-text hover:border-terminal-amber/40 hover:text-terminal-amber lg:hidden",
        className,
      )}
      aria-label="Open menu"
    >
      <Menu className="h-4 w-4" />
    </button>
  );
}
