# Poly Meridian — STATUS

Last updated: 2026-05-23

## Phase 7a — Operator Dashboard ✅ (skeleton complete, awaiting local verification)

Bloomberg-style web UI on top of the now-complete backend. Modern dark
aesthetic, dense data layout, real-time updates via Server-Sent Events.

### Done

**Backend API extension** ([`src/poly_meridian/api/`](src/poly_meridian/api))
- [`app.py`](src/poly_meridian/api/app.py) — FastAPI app: `/health`, `/api/state`, `/api/settings`, `/api/stream` (SSE), `/api/kill-switch/(en|dis)gage`. CORS enabled for the web service.
- [`state.py`](src/poly_meridian/api/state.py) — `AgentStateBroker`: in-process pub/sub. Holds latest `Snapshot`, multiplexes events to SSE subscribers, decoupled from trading loop (slow dashboard never blocks orders).
- Added `fastapi>=0.115` + `uvicorn[standard]>=0.32` to base deps.
- [`main.py`](src/poly_meridian/main.py) — replaced stdlib `/health` handler with FastAPI via uvicorn. New `_broker_refresh_loop` pushes portfolio + kill-switch state every 5s.

**Web app** ([`web/`](web))
- **Next.js 15 + React 19 + TypeScript + Tailwind v3** scaffold (Next 15 still requires Tailwind v3 at the moment).
- Bloomberg-inspired palette in [`tailwind.config.ts`](web/tailwind.config.ts):
  - Background `#0a0a0b`, surface `#111114`, alt `#16161a`
  - **Amber accent `#ff9e0a`** (signature)
  - Status colors: green `#22c55e`, red `#ef4444`, yellow `#eab308`, cyan `#22d3ee`, purple `#a855f7`
  - Geist Sans for UI, JetBrains Mono for all data
- [`app/page.tsx`](web/app/page.tsx) — main dashboard, 2×3 panel grid, footer with hotkey hints
- [`components/header-bar.tsx`](web/components/header-bar.tsx) — NAV, cash, daily P&L, exposure, open positions, markets watched, mode pill, connection status, kill-switch button
- 6 panels, each with [`Panel`](web/components/panel.tsx) chrome (hotkey badge + title + subtitle + scrollable body):
  - [`positions-table.tsx`](web/components/positions-table.tsx) — token, qty, avg cost, mark, P&L, notional
  - [`signals-feed.tsx`](web/components/signals-feed.tsx) — time, strategy (color-coded), action, condition, edge
  - [`orders-feed.tsx`](web/components/orders-feed.tsx) — time, strategy, side, price, size, status, mode
  - [`smart-money-panel.tsx`](web/components/smart-money-panel.tsx) — tier badge, direction, cluster size, condition, net USD, recency
  - [`strategies-panel.tsx`](web/components/strategies-panel.tsx) — per-strategy signal count + share bar; disabled strategies dimmed
  - [`risk-panel.tsx`](web/components/risk-panel.tsx) — daily P&L vs cap, total exposure vs cap, open positions vs 50; color-graded meters
- [`status-pill.tsx`](web/components/status-pill.tsx) — reusable status indicator with `ok|warn|alert|info|neutral` tones; alert variant blinks
- Real-time data:
  - [`hooks/use-agent-state.ts`](web/hooks/use-agent-state.ts) — initial REST fetch + SSE subscription; reducer-style updates for snapshot/signal/order/cluster/kill_switch events
  - [`lib/api.ts`](web/lib/api.ts) — REST client (fetchState, engageKillSwitch, disengageKillSwitch, streamUrl)
  - [`lib/types.ts`](web/lib/types.ts) — shared TS types matching backend Snapshot
- Keyboard navigation: `1-6` jumps to panels (scroll into view), `K` toggles kill-switch (with confirm)
- Tabular numerics + monospace data, subtle scrollbars, blinking alerts
- Railway-ready: [`railway.json`](web/railway.json) for separate service deploy

### Phase 7a acceptance gate
| Check | Status | Notes |
|---|---|---|
| Backend has structured `/api/state` + SSE stream | ✅ | FastAPI mounted on prometheus_port (8000), broker decoupled from trading loop |
| Risk gate still untouched | ✅ | Pipeline is mode/UI agnostic; UI is read-only + kill-switch toggle (which already exists in policy) |
| UI bootstrap fetch + live SSE | ✅ | `use-agent-state.ts` handles both paths; replays last 50 events on subscribe |
| Bloomberg-style design (dark / dense / amber / mono) | ✅ | Tailwind theme tokens + components honor the aesthetic |
| Keyboard nav for power users | ✅ | 1-6 jumps panels, K toggles kill-switch |
| `make test` passes (43 test files) | ⏳ | Web has no Python tests; backend Python tests unchanged |
| `npm run dev` brings up the UI | ⏳ | Requires `cd web && npm install && npm run dev` (user-side) |

### Operator action items
1. **Install Node.js** (≥18) if not already; install web deps:
   ```bash
   cd web
   npm install
   cp .env.example .env.local
   npm run dev      # → http://localhost:3001
   ```
2. **Run the agent** (`make up` or `python -m poly_meridian.main`). The UI auto-connects to `http://localhost:8000`. **Important:** the agent must be re-installed first (`uv pip install -e .`) so the new `fastapi` + `uvicorn` deps are picked up.
3. **Deploy to Railway:**
   - Create new service in the same Railway project.
   - Root directory: `web/`.
   - Env vars: `NEXT_PUBLIC_API_URL=https://<agent-service>.up.railway.app`.
   - Railway picks up `web/railway.json`.

### Deferred to Phase 7b/c
- Interactive controls beyond kill-switch (strategy enable/disable, position close, drill toggles)
- Charts (NAV equity curve, per-strategy P&L attribution) — Lightweight Charts or visx
- Settings page (read/write `config/*.yaml`)
- Light theme toggle (the brave can ask)
- Auth (Phase 8 if multi-operator)
- More dashboards: backtest report viewer, walk-forward fold browser, news feed

## Overall progress

| Phase | Durum |
|---|---|
| 0-5b | ✅ backend complete |
| 6 Live executor + promotion | ✅ code-side done |
| **7a Operator Dashboard** | ✅ skeleton + 6 panels |
| 7b Interactive controls + charts | 🔄 next iteration |
| 7c Settings + multi-page | ⏳ later |

## Open questions
1. **First UI test:** Want me to walk through bringing it up locally once you have Node installed?
2. **Charts library preference:** Lightweight Charts (TradingView's) vs visx vs Tremor? Phase 7b decision.
3. **Mobile layout:** today the grid collapses to single-column on small screens; do you want a proper mobile view in 7c?
