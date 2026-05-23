# Poly Meridian — Web Dashboard

Bloomberg-style operator UI for the [Poly Meridian](../README.md) trading agent.

**Stack:** Next.js 15 · React 19 · TypeScript · Tailwind CSS v3 · Server-Sent Events

## Design

- Dark, dense, multi-panel layout
- Modern Bloomberg-Terminal aesthetic — amber accent (`#ff9e0a`) over near-black surfaces
- Tabular numerics + JetBrains Mono for data
- Keyboard-first navigation (1-6 jumps panels, K toggles kill-switch)

## Local development

```bash
cd web
npm install
cp .env.example .env.local
# Edit .env.local — point NEXT_PUBLIC_API_URL at the running agent
# (default: http://localhost:8000, which is the agent's FastAPI port)

npm run dev      # http://localhost:3001
```

The agent must be running with the FastAPI server active (`make up` or
`python -m poly_meridian.main`). The dashboard fetches `/api/state` once
on boot, then subscribes to `/api/stream` (SSE) for real-time updates.

## Panels

| # | Panel | Hotkey |
|---|-------|--------|
| 1 | Open positions table | `1` |
| 2 | Strategy signals feed | `2` |
| 3 | Recent orders log | `3` |
| 4 | Smart-money clusters | `4` |
| 5 | Strategies activity | `5` |
| 6 | Risk & limits | `6` |
| — | Toggle kill-switch | `K` |

## Production deploy (Railway)

This directory has its own [`railway.json`](railway.json) so it deploys as a
separate Railway service alongside the agent:

1. New service in your Railway project → Deploy from GitHub.
2. Set root directory to `web/`.
3. Environment variables:
   - `NEXT_PUBLIC_API_URL` → public URL of your agent service (e.g. `https://poly-meridian.up.railway.app`)
   - `AGENT_API_URL` → same (for SSR fetches if any).
4. Railway runs `npm run build` then `npm start` on `PORT`.

## Project layout

```
web/
├── app/
│   ├── layout.tsx           # root layout, fonts
│   ├── page.tsx             # main dashboard
│   └── globals.css          # Tailwind + theme tokens
├── components/
│   ├── header-bar.tsx       # NAV, P&L, mode, kill-switch
│   ├── panel.tsx            # shared panel chrome
│   ├── status-pill.tsx      # status indicator
│   ├── positions-table.tsx
│   ├── signals-feed.tsx
│   ├── orders-feed.tsx
│   ├── smart-money-panel.tsx
│   ├── strategies-panel.tsx
│   └── risk-panel.tsx
├── hooks/
│   └── use-agent-state.ts   # initial fetch + SSE subscription
├── lib/
│   ├── api.ts               # REST client
│   ├── types.ts             # shared types
│   └── utils.ts             # formatters + cn()
├── package.json
├── tailwind.config.ts
├── tsconfig.json
└── next.config.js
```
