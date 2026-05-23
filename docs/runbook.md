# Poly Meridian — Operational Runbook

This is the operator-facing reference: backup, restore, secret rotation,
kill-switch, paper→live promotion. Master Spec §23 is the source of truth.

---

## 1. Backup procedure (daily, automated)

```bash
# Inside the agent container
docker compose exec -T db pg_dump \
    --format=custom --no-owner --no-privileges \
    --file=/backups/poly_meridian-$(date +%Y%m%d).dump \
    -U poly poly_meridian
```

Schedule via Railway cron or your scheduler. Retention: 30 days.

---

## 2. Restore procedure

```bash
# 1. Spin up a fresh DB instance
docker compose exec -T db createdb -U poly poly_meridian_restore_test

# 2. Restore
docker compose exec -T db pg_restore \
    --dbname=poly_meridian_restore_test \
    /backups/poly_meridian-<DATE>.dump

# 3. Sanity check
docker compose exec -T db psql -U poly -d poly_meridian_restore_test \
    -c 'SELECT count(*) FROM markets;'

# 4. If green, swap or rename. If red, fix backup pipeline first.
docker compose exec -T db dropdb -U poly poly_meridian_restore_test
```

Rehearse this **at least once per quarter** (MASTER_SPEC §19 checklist
item).

---

## 3. Secret rotation (Polymarket private key + API)

1. Generate new wallet **offline**. Record the address.
2. Test transfer: send $10 USDC from old wallet to new. Confirm arrival on
   PolygonScan.
3. Move remaining USDC to new wallet (split if large).
4. Update Railway env vars:
   - `POLYMARKET_PRIVATE_KEY` → new key
   - Leave `POLYMARKET_API_KEY/SECRET/PASSPHRASE` empty — agent re-derives on
     first L1 sign-in.
5. Redeploy. Watch logs for `clob.authed.deferred` → then for derived
   credentials saved successfully.
6. Confirm old wallet balance = 0. Decommission its private key (delete
   from password manager).

**Frequency:** every 6 months or after any suspected exposure.

---

## 4. Kill-switch — manual engage / disengage

Engage paths (any of these):
- Automated: kill_switch observes daily P&L, API error rate, slippage
  anomaly, WS disconnect grace, wallet balance mismatch (all live in
  `risk/kill_switch.py`).
- Manual: operator runs:
  ```bash
  docker compose exec -T agent python -c \
    "from poly_meridian.risk import DefaultRiskPolicy; \
     p = DefaultRiskPolicy(strategy_name='ops'); \
     p.kill_switch.manual_engage('operator stopped trading')"
  ```
  (Phase 6 adds a Slack-trigger endpoint for ergonomics.)

Verify:
- `pm_kill_switch_engaged` Prometheus gauge → 1
- Grafana Overview "Kill-switch" panel → red "ENGAGED"
- No `pipeline.order` log lines for 5 minutes

Disengage:
- Manual only (no automatic re-arm). Operator must investigate root cause
  first.
- Re-run agent or call `p.kill_switch.disengage()` from a Python shell.

---

## 5. Paper → Live promotion (MASTER_SPEC §19)

Run [`scripts/promote_to_live.py`](../scripts/promote_to_live.py). All 10
checklist items must be ✅. Notable items:

- Paper run ≥30 days successful (real WS data, virtual NAV $100K)
- Paper Sharpe > 1.2, Max DD < 20%
- Kill-switch drill done at least once
- DR drill done at least once
- 24h+ uptime drill (reconnect + restart)
- Slack/Telegram alerts live
- Initial live capital ≤ 5% of paper NAV

After PASS, flip `MODE=live-conservative` in Railway env vars and redeploy.

---

## 6. Chaos drill (Phase 6 pre-live)

Run before live promotion. Simulates failures:
- DB disconnect for 5 minutes → agent should warn but stay up, kill-switch
  engages on prolonged outage
- WS disconnect for 2 minutes → graceful reconnect with exponential backoff
- Gamma 503 burst → tenacity retries, eventually shrugs and continues
- RPC fail (Polygon) → onchain provider warns, smart-money strategy idles

Pass criteria: agent process never crashes, kill-switch engages where
warranted, Grafana shows the right metrics, no orders leak during outage.

Automate with [`tests/chaos/test_chaos_drills.py`](../tests/chaos/test_chaos_drills.py)
where possible; manual for the infra-level failures.

---

## 7. Disaster recovery drill ([`scripts/dr_drill.py`](../scripts/dr_drill.py))

Interactive operator drill. Run it before paper→live promotion. Walks
through backup, restore, secret rotation, kill-switch, health checks.
Each step requires explicit operator confirmation — the point is
muscle memory, not just automation.

---

## 8. Incident response — first 30 minutes

If you notice unexpected drawdown or behaviour:

1. **Engage kill-switch immediately** (manual, see §4). Stops new orders.
2. Check Grafana Overview: NAV trend, open positions, kill-switch state.
3. Check structlog JSON output: look for `risk.reject` reasons, `paper.fill`
   anomalies, `agent.error_loop` patterns.
4. If positions need closing: use Polymarket UI directly with the wallet
   key — don't try to fix code under stress.
5. After incident: postmortem doc with timeline, root cause, fix, drill
   update. Add to `docs/incidents/<YYYY-MM-DD>-<slug>.md`.
