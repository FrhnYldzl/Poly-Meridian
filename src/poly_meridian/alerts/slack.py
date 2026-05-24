"""Slack webhook dispatcher — post operator notifications.

Design:
  - `post_slack_alert(message, level)` — sync wrapper, spawns background task.
  - `slack_alert_async(message, level)` — actual httpx POST. 5s timeout.
  - No-op (returns False) when SLACK_WEBHOOK_URL is unset — so the agent
    runs identically on Railway whether Slack is wired or not.
  - Fire-and-forget: alert latency never blocks the trading loop.

The "drill" portion (per Phase A.3): the agent emits 4 alert types:
  • boot — "Poly Meridian started"
  • kill_switch — engage/disengage
  • first_signal — first paper signal of the session
  • first_fill — first paper fill of the session

Session-first tracking lives in `alerts.state.AlertState` (in-process only;
restarting the agent resets the "first" flags).
"""
from __future__ import annotations

import asyncio
from typing import Final

import httpx
import structlog

from poly_meridian.settings import get_settings

log = structlog.get_logger("poly_meridian.alerts.slack")

_TIMEOUT: Final[float] = 5.0

# Level → emoji prefix for Slack messages.
_LEVEL_EMOJI: Final[dict[str, str]] = {
    "info": ":large_green_circle:",
    "warn": ":large_yellow_circle:",
    "error": ":red_circle:",
    "signal": ":dart:",
    "fill": ":briefcase:",
}


async def slack_alert_async(message: str, *, level: str = "info") -> bool:
    """Post a single message to the configured Slack webhook.

    Returns True on success (HTTP 2xx), False otherwise — including the
    common "not configured" path. The agent does not error if Slack is
    unreachable; we log a warning and move on.
    """
    settings = get_settings()
    url = settings.slack_webhook_url.get_secret_value()
    if not url:
        return False

    emoji = _LEVEL_EMOJI.get(level, _LEVEL_EMOJI["info"])
    payload = {
        "text": f"{emoji} *poly-meridian* · {message}",
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(url, json=payload)
        if r.status_code >= 300:
            log.warning(
                "alerts.slack.bad_status",
                status=r.status_code,
                body=r.text[:200],
            )
            return False
        return True
    except Exception as exc:
        log.warning("alerts.slack.send_failed", error=str(exc))
        return False


def post_slack_alert(message: str, *, level: str = "info") -> None:
    """Fire-and-forget Slack post — never blocks the caller.

    Safe to call from sync contexts (the agent's signal handler, kill-switch
    hook, etc.). If we're inside an asyncio event loop, the task is scheduled
    there; otherwise a fresh loop runs the coroutine to completion.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        loop.create_task(slack_alert_async(message, level=level))
        return

    # No running loop — run synchronously. Used from CLI / startup hooks.
    try:
        asyncio.run(slack_alert_async(message, level=level))
    except Exception as exc:
        log.warning("alerts.slack.sync_run_failed", error=str(exc))
