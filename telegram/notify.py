from __future__ import annotations

import http.client
import json
import os
import sys
import urllib.request

from telegram.templates import render


def _http_post(url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=15).read() or b"{}")


class Notifier:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run

    def send(self, template: str, **ctx) -> int:
        text = render(template, **ctx)
        if self.dry_run:
            print(f"[dry-run] telegram {template}: {text}")
            return 0
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            print("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set; dropping "
                  f"notification: {template}", file=sys.stderr)
            return 0
        try:
            resp = _http_post(f"https://api.telegram.org/bot{token}/sendMessage",
                              {"chat_id": chat_id, "text": text})
            return int((resp.get("result") or {}).get("message_id", 0))
        except (OSError, http.client.HTTPException, ValueError) as e:
            print(f"telegram send failed: {e}", file=sys.stderr)
            return 0
