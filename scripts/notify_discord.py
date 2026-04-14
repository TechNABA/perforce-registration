#!/usr/bin/env python3
"""
notify_discord.py — Send a Discord webhook notification for new registrations.
Called by the register.yml GitHub Actions workflow.

Reads from environment variables:
  DISCORD_WEBHOOK  — the webhook URL
  USER_DATA        — JSON string (single user or array)
"""

import json
import os
import sys
import urllib.request
import urllib.error


def main():
    webhook = os.environ.get("DISCORD_WEBHOOK", "").strip()
    raw = os.environ.get("USER_DATA", "").strip()

    if not webhook:
        print("No DISCORD_WEBHOOK set — skipping notification.")
        return

    if not raw:
        print("No USER_DATA set — skipping notification.")
        return

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Invalid JSON: {e}")
        sys.exit(1)

    users = parsed if isinstance(parsed, list) else [parsed]

    lines = []
    for u in users:
        name = u.get("full_name", "Unknown")
        uname = u.get("username", "unknown")
        team = u.get("team", "N/A")
        lines.append(f"\u2022 **{name}** (`{uname}`) \u2014 {team}")

    team = users[0].get("team", "N/A")
    tesista = users[0].get("tesista", "no").capitalize()
    count = str(len(users))

    payload = {
        "embeds": [
            {
                "title": "\U0001f4cb Nuova registrazione Perforce",
                "description": "\n".join(lines),
                "color": 2664261,
                "fields": [
                    {"name": "Utenti", "value": count, "inline": True},
                    {"name": "Team", "value": team, "inline": True},
                    {"name": "Tesista", "value": tesista, "inline": True},
                ],
                "footer": {"text": "Perforce NABA Registration System"},
            }
        ]
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "NABA-Perforce-Bot/1.0",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req) as resp:
            print(f"Discord notification sent (status {resp.status})")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"Discord webhook error {e.code}: {body}")
        # Don't fail the workflow for a notification error
        sys.exit(0)


if __name__ == "__main__":
    main()