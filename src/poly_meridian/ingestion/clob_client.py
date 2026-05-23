"""Polymarket CLOB REST + authed client. §11.2 + §16.4.

Two surfaces:
  - **Read-only** (no auth) — `server_time()`, `book_snapshot()`, `midpoint()`.
    Always available via plain httpx.
  - **Authed** — order create/post/cancel + user state. Requires
    `py-clob-client` installed and `POLYMARKET_PRIVATE_KEY` set.

Auth flow per §11.2:
  L1 (EIP-712 wallet sig) → derive_api_creds → L2 (HMAC-SHA256)

The exact method names on `py-clob-client` have varied across releases.
We isolate that behind `_try_import_clob()` + try-multiple-method-names
in `LiveExecutor` so upgrading the library = touch one function, not the
whole executor.

NOTE: verify against the installed `py-clob-client` version. First-time
operator should boot the agent in paper mode, set POLYMARKET_PRIVATE_KEY,
and check the logs for `clob.authed.init_ok` before flipping to live.
"""
from __future__ import annotations

from typing import Any

import httpx
import structlog

from poly_meridian.settings import get_settings

log = structlog.get_logger("poly_meridian.clob")


def _try_import_clob() -> tuple[Any, Any] | tuple[None, None]:
    """Returns (lib_module, ClobClient_class) or (None, None) on failure."""
    for module_name in ("py_clob_client_v2", "py_clob_client"):
        try:
            lib = __import__(module_name)
        except ImportError:
            continue
        # Most versions expose ClobClient on `<module>.client.ClobClient`.
        try:
            ClobClient = __import__(f"{module_name}.client", fromlist=["ClobClient"]).ClobClient
        except (ImportError, AttributeError):
            try:
                ClobClient = lib.ClobClient    # type: ignore[attr-defined]
            except AttributeError:
                continue
        log.info("clob.lib", module=module_name)
        return lib, ClobClient
    return None, None


class ClobClient:
    """Polymarket CLOB client. Read-only methods always work.

    `init_authed()` brings up the authed library client. Order submission +
    cancellation route through that — see `LiveExecutor`.
    """

    def __init__(self, base_url: str | None = None, timeout_sec: float = 15.0) -> None:
        s = get_settings()
        self._base = (base_url or s.polymarket_clob_host).rstrip("/")
        self._timeout = timeout_sec
        self._http: httpx.AsyncClient | None = None
        self._lib, self._ClobClientCls = _try_import_clob()
        self._authed: Any = None

    async def start(self) -> None:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self._base, timeout=self._timeout,
                headers={"User-Agent": "poly-meridian/0.1"},
            )

    async def stop(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # ---------- Public, read-only ----------

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

    # ---------- Authed ----------

    def has_authed_client(self) -> bool:
        return self._authed is not None

    def authed(self) -> Any:
        """Return the underlying py-clob-client instance. Raises if not initialized."""
        if self._authed is None:
            raise RuntimeError("authed client not initialized — call init_authed() first")
        return self._authed

    def init_authed(self) -> bool:
        """Initialize the authenticated client. Idempotent.

        Returns True if authed surface is ready, False if degraded (no library
        or no key).
        """
        if self._authed is not None:
            return True
        if self._ClobClientCls is None:
            log.warning("clob.no_library",
                        msg="py-clob-client not installed — authed paths disabled")
            return False
        s = get_settings()
        pk = s.polymarket_private_key.get_secret_value()
        if not pk:
            log.warning("clob.no_private_key",
                        msg="POLYMARKET_PRIVATE_KEY missing — authed paths disabled")
            return False

        api_key = s.polymarket_api_key.get_secret_value() or None
        api_secret = s.polymarket_api_secret.get_secret_value() or None
        passphrase = s.polymarket_passphrase.get_secret_value() or None

        try:
            if api_key and api_secret and passphrase:
                creds = self._build_creds(api_key, api_secret, passphrase)
                self._authed = self._ClobClientCls(
                    host=s.polymarket_clob_host,
                    chain_id=s.polymarket_chain_id,
                    key=pk,
                    creds=creds,
                )
            else:
                self._authed = self._ClobClientCls(
                    host=s.polymarket_clob_host,
                    chain_id=s.polymarket_chain_id,
                    key=pk,
                )
                self._derive_and_warn(s)
        except Exception as exc:
            log.error("clob.authed.init_failed", error=str(exc))
            self._authed = None
            return False

        log.info("clob.authed.init_ok")
        return True

    def _build_creds(self, key: str, secret: str, passphrase: str) -> Any:
        if self._lib is None:
            return None
        for attr in ("ApiCreds", "Credentials"):
            cls = getattr(self._lib, attr, None) or getattr(
                getattr(self._lib, "clob_types", None), attr, None
            )
            if cls is None:
                continue
            try:
                return cls(api_key=key, api_secret=secret, api_passphrase=passphrase)
            except TypeError:
                try:
                    return cls(key, secret, passphrase)
                except Exception:
                    continue
        log.warning("clob.creds.unknown_shape",
                    msg="library has no ApiCreds/Credentials class — passing raw dict")
        return {"api_key": key, "api_secret": secret, "api_passphrase": passphrase}

    def _derive_and_warn(self, s: Any) -> None:
        for method_name in ("create_or_derive_api_creds", "derive_api_key"):
            method = getattr(self._authed, method_name, None)
            if method is None:
                continue
            try:
                creds = method()
                log.warning(
                    "clob.creds.derived",
                    msg="API creds derived — copy these to .env to skip derivation next boot",
                    method=method_name,
                    creds_type=type(creds).__name__,
                )
                return
            except Exception as exc:
                log.warning("clob.derive_failed", method=method_name, error=str(exc))
        log.warning("clob.no_derive_method",
                    msg="library doesn't expose create_or_derive_api_creds — manual generation required")
