"""Polymarket CLOB REST client — auth + order submission. §11.2 + §16.4.

Thin wrapper around `py-clob-client`. Phase 1 only exercises the public
read paths (markets, book snapshot, server time). Order submission lands
in Phase 2 once the PaperExecutor is in place and Phase 6 for live mode.

The library has had two import paths in the wild: `py_clob_client` (v1)
and `py_clob_client_v2`. We try v2 first, then fall back. If neither is
present, this module degrades gracefully — read-only endpoints still
work via plain httpx.
"""
from __future__ import annotations

from typing import Any

import httpx
import structlog

from poly_meridian.settings import get_settings

log = structlog.get_logger("poly_meridian.clob")


def _try_import_clob() -> Any | None:
    try:
        import py_clob_client_v2  # type: ignore[import-not-found]
        return py_clob_client_v2
    except ImportError:
        pass
    try:
        import py_clob_client  # type: ignore[import-not-found]
        return py_clob_client
    except ImportError:
        return None


class ClobClient:
    """Polymarket CLOB client. Read-only methods always work; authed
    methods require `py-clob-client` to be installed and credentials set.

    The actual order-submission integration is wired in Phase 2 (paper)
    and Phase 6 (live) — this class is the stable surface those phases
    will call.
    """

    def __init__(self, base_url: str | None = None, timeout_sec: float = 15.0) -> None:
        s = get_settings()
        self._base = (base_url or s.polymarket_clob_host).rstrip("/")
        self._timeout = timeout_sec
        self._http: httpx.AsyncClient | None = None
        self._lib = _try_import_clob()
        self._authed_client: Any = None

    async def start(self) -> None:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self._base,
                timeout=self._timeout,
                headers={"User-Agent": "poly-meridian/0.1"},
            )

    async def stop(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # ---------- Public, read-only (httpx) ----------

    async def server_time(self) -> int:
        assert self._http is not None
        r = await self._http.get("/time")
        r.raise_for_status()
        return int(r.text.strip())

    async def book_snapshot(self, token_id: str) -> dict[str, Any]:
        assert self._http is not None
        r = await self._http.get("/book", params={"token_id": token_id})
        r.raise_for_status()
        return r.json()

    async def midpoint(self, token_id: str) -> float | None:
        assert self._http is not None
        r = await self._http.get("/midpoint", params={"token_id": token_id})
        if r.status_code != 200:
            return None
        data = r.json()
        try:
            return float(data["mid"]) if isinstance(data, dict) else float(data)
        except (KeyError, TypeError, ValueError):
            return None

    # ---------- Authed (delegated to py-clob-client) ----------

    def has_authed_client(self) -> bool:
        return self._authed_client is not None

    def init_authed(self) -> None:
        """Initialize the authenticated client. Idempotent. Phase 2/6 wires this in."""
        if self._authed_client is not None:
            return
        if self._lib is None:
            log.warning("clob.no_library", msg="py-clob-client not installed; authed paths disabled")
            return
        s = get_settings()
        pk = s.polymarket_private_key.get_secret_value()
        if not pk:
            log.warning("clob.no_private_key", msg="POLYMARKET_PRIVATE_KEY missing; authed paths disabled")
            return
        # Concrete construction is library-version-specific — Phase 2 nails this down.
        log.info("clob.authed.deferred", lib=self._lib.__name__)
