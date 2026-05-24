"""Alert dispatch layer — Slack/Telegram webhooks for operator notifications.

Tiny by design: post a string, get a bool back. Fire-and-forget pattern so
network latency on the alert channel never blocks the trading loop.
"""
from poly_meridian.alerts.slack import post_slack_alert, slack_alert_async

__all__ = ["post_slack_alert", "slack_alert_async"]
