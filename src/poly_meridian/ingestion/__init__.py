"""Data ingestion layer — Gamma, CLOB, WebSocket, news, twitter, on-chain. §11."""
from poly_meridian.ingestion.base import IngestionSource
from poly_meridian.ingestion.clob_client import ClobClient
from poly_meridian.ingestion.clob_ws import ClobWebsocketSource
from poly_meridian.ingestion.gamma_client import GammaClient
from poly_meridian.ingestion.news_provider import GdeltNewsSource

__all__ = [
    "ClobClient",
    "ClobWebsocketSource",
    "GammaClient",
    "GdeltNewsSource",
    "IngestionSource",
]
