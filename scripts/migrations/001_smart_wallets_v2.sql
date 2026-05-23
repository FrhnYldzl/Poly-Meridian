-- Migration 001 — smart_wallets v2 columns. See MASTER_SPEC v1.1 §12 + §14.3.
-- Idempotent (ALTER ... IF NOT EXISTS / DROP CONSTRAINT IF EXISTS).
--
-- Apply:  docker compose exec -T db psql -U poly -d poly_meridian \
--             < scripts/migrations/001_smart_wallets_v2.sql

BEGIN;

ALTER TABLE smart_wallets
    ADD COLUMN IF NOT EXISTS tier            INT NOT NULL DEFAULT 3,
    ADD COLUMN IF NOT EXISTS category_focus  TEXT,
    ADD COLUMN IF NOT EXISTS last_7d_pnl     NUMERIC,
    ADD COLUMN IF NOT EXISTS recency_score   NUMERIC NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS hedge_flag      BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS drawdown_7d_pct NUMERIC;

-- Re-add tier CHECK constraint defensively.
ALTER TABLE smart_wallets DROP CONSTRAINT IF EXISTS smart_wallets_tier_check;
ALTER TABLE smart_wallets
    ADD CONSTRAINT smart_wallets_tier_check CHECK (tier IN (1, 2, 3));

CREATE INDEX IF NOT EXISTS idx_smart_wallets_tier ON smart_wallets(tier, last_updated DESC);
CREATE INDEX IF NOT EXISTS idx_smart_wallets_category ON smart_wallets(category_focus);

COMMIT;
