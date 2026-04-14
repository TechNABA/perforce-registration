#!/usr/bin/env python3
"""
discord_email_provision.py

After Perforce provisioning, this module:
  1. Creates a Discord role named after the team (if it doesn't exist)
  2. Creates a private text channel under the specified category
  3. Sets permissions: only that role can see/write in the channel
  4. Generates a one-time invite link to the channel
  5. Sends a welcome email to each user with:
     - Confirmation of Perforce account creation
     - Discord invite link
     - Depot/team info

Can be used standalone or called from perforce_provision.py.

Usage (standalone):
    python discord_email_provision.py --csv data/users.csv --dry-run
    python discord_email_provision.py --csv data/users.csv

The script will interactively ask for:
  - Discord bot token
  - SMTP password
"""

import argparse
import csv
import getpass
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path


# ══════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════

# Discord
DISCORD_GUILD_ID = "1369620948680048710"  # Right-click server → Copy Server ID
DISCORD_CATEGORY_NAME = "Tesi"                # Default category for new channels

# Resend (https://resend.com — free: 100 emails/day)
# Use "onboarding@resend.dev" for testing, then add your own domain
RESEND_FROM = "NABA Perforce <noreply@p4naba.com>"

# Email template subject
EMAIL_SUBJECT_IT = "Account Perforce creato — {team}"
EMAIL_SUBJECT_EN = "Perforce account created — {team}"

# ══════════════════════════════════════════════════════════════

DISCORD_API = "https://discord.com/api/v10"
FIELDS = [
    "timestamp", "username", "full_name", "email",
    "team", "tesista", "anno_corso", "status",
]


# ── Discord API helpers ─────────────────────────────────────
def discord_request(method: str, endpoint: str, token: str, data: dict = None) -> dict | None:
    """Make a Discord API request."""
    url = f"{DISCORD_API}{endpoint}"
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
        "User-Agent": "NABA-Perforce-Bot/1.0",
    }

    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req) as resp:
            if resp.status == 204:
                return {}
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        print(f"    [Discord ERROR] {e.code}: {error_body}")
        return None


def get_guild_roles(token: str) -> list[dict]:
    """Get all roles in the guild."""
    return discord_request("GET", f"/guilds/{DISCORD_GUILD_ID}/roles", token) or []


def get_guild_channels(token: str) -> list[dict]:
    """Get all channels in the guild."""
    return discord_request("GET", f"/guilds/{DISCORD_GUILD_ID}/channels", token) or []


def find_category_id(token: str, category_name: str) -> str | None:
    """Find a category channel by name."""
    channels = get_guild_channels(token)
    for ch in channels:
        # type 4 = category
        if ch.get("type") == 4 and ch.get("name", "").lower() == category_name.lower():
            return ch["id"]
    return None


def find_role_by_name(token: str, name: str) -> dict | None:
    """Find a role by exact name."""
    roles = get_guild_roles(token)
    for r in roles:
        if r.get("name", "").lower() == name.lower():
            return r
    return None


def find_channel_by_name(token: str, name: str, parent_id: str = None) -> dict | None:
    """Find a text channel by name, optionally within a category."""
    channels = get_guild_channels(token)
    for ch in channels:
        if ch.get("type") == 0 and ch.get("name", "").lower() == name.lower():
            if parent_id is None or ch.get("parent_id") == parent_id:
                return ch
    return None


def create_role(token: str, team_name: str, dry_run: bool = False) -> dict | None:
    """Create a Discord role for the team."""
    existing = find_role_by_name(token, team_name)
    if existing:
        print(f"    [skip] Discord role '{team_name}' already exists")
        return existing

    if dry_run:
        print(f"    [dry-run] Would create Discord role '{team_name}'")
        return {"id": "dry-run", "name": team_name}

    # Generate a color based on team name hash (for visual distinction)
    color = hash(team_name) % 0xFFFFFF

    role = discord_request("POST", f"/guilds/{DISCORD_GUILD_ID}/roles", token, {
        "name": team_name,
        "color": color,
        "mentionable": True,
    })

    if role:
        print(f"    [created] Discord role '{team_name}'")
    return role


def create_channel(token: str, team_name: str, role_id: str, category_id: str = None, dry_run: bool = False) -> dict | None:
    """Create a private text channel visible only to the team role."""
    # Channel names in Discord are lowercase, no spaces
    channel_name = team_name.lower().replace(" ", "-")

    existing = find_channel_by_name(token, channel_name, category_id)
    if existing:
        print(f"    [skip] Discord channel '#{channel_name}' already exists")
        return existing

    if dry_run:
        print(f"    [dry-run] Would create Discord channel '#{channel_name}'")
        return {"id": "dry-run", "name": channel_name}

    # Get @everyone role id (same as guild id)
    everyone_role_id = DISCORD_GUILD_ID

    # Permission overwrites:
    # - @everyone: deny VIEW_CHANNEL (0x400)
    # - team role: allow VIEW_CHANNEL + SEND_MESSAGES + READ_MESSAGE_HISTORY
    permission_overwrites = [
        {
            "id": everyone_role_id,
            "type": 0,  # role
            "deny": "1024",  # VIEW_CHANNEL
            "allow": "0",
        },
        {
            "id": role_id,
            "type": 0,  # role
            "allow": "68608",  # VIEW_CHANNEL (1024) + SEND_MESSAGES (2048) + READ_MESSAGE_HISTORY (65536)
            "deny": "0",
        },
    ]

    payload = {
        "name": channel_name,
        "type": 0,  # text channel
        "permission_overwrites": permission_overwrites,
    }

    if category_id:
        payload["parent_id"] = category_id

    channel = discord_request("POST", f"/guilds/{DISCORD_GUILD_ID}/channels", token, payload)

    if channel:
        print(f"    [created] Discord channel '#{channel_name}'")
    return channel


def create_invite(token: str, channel_id: str, dry_run: bool = False) -> str | None:
    """Create a permanent invite link for a channel."""
    if dry_run:
        print(f"    [dry-run] Would create invite for channel")
        return "https://discord.gg/dry-run-invite"

    invite = discord_request("POST", f"/channels/{channel_id}/invites", token, {
        "max_age": 0,       # never expires
        "max_uses": 0,      # unlimited uses
        "unique": False,    # reuse existing if possible
    })

    if invite and "code" in invite:
        url = f"https://discord.gg/{invite['code']}"
        print(f"    [invite] {url}")
        return url

    return None


# ── Email helpers ───────────────────────────────────────────
def build_email_html(user: dict, team: str, invite_url: str) -> str:
    """Build a beautiful HTML email."""
    return f"""
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f5f5f7;font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f7;padding:40px 20px;">
<tr><td align="center">
<table width="580" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:16px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,0.08);">

<!-- Header -->
<tr><td style="background:#1c1c1e;padding:32px 40px;text-align:center;">
  <h1 style="margin:0;color:#f5f5f7;font-size:22px;font-weight:600;">NABA Perforce</h1>
  <p style="margin:8px 0 0;color:#a1a1a6;font-size:14px;">Account creato con successo</p>
</td></tr>

<!-- Body -->
<tr><td style="padding:40px;">
  <p style="margin:0 0 20px;font-size:16px;color:#1d1d1f;line-height:1.6;">
    Ciao <strong>{user['full_name']}</strong>,
  </p>
  <p style="margin:0 0 24px;font-size:16px;color:#1d1d1f;line-height:1.6;">
    Il tuo account Perforce è stato creato. Ecco i tuoi dati:
  </p>

  <!-- Info card -->
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f7;border-radius:12px;padding:24px;margin:0 0 24px;">
  <tr><td style="padding:24px;">
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td style="padding:6px 0;font-size:13px;color:#86868b;width:140px;">Username</td>
        <td style="padding:6px 0;font-size:15px;color:#1d1d1f;font-weight:500;font-family:'SF Mono',Menlo,monospace;">{user['username']}</td>
      </tr>
      <tr>
        <td style="padding:6px 0;font-size:13px;color:#86868b;">Server</td>
        <td style="padding:6px 0;font-size:15px;color:#1d1d1f;font-weight:500;font-family:'SF Mono',Menlo,monospace;">perforce.naba.it:1666</td>
      </tr>
      <tr>
        <td style="padding:6px 0;font-size:13px;color:#86868b;">Team / Progetto</td>
        <td style="padding:6px 0;font-size:15px;color:#1d1d1f;font-weight:500;">{team}</td>
      </tr>
      <tr>
        <td style="padding:6px 0;font-size:13px;color:#86868b;">Depot</td>
        <td style="padding:6px 0;font-size:15px;color:#1d1d1f;font-weight:500;font-family:'SF Mono',Menlo,monospace;">//{team}/...</td>
      </tr>
    </table>
  </td></tr>
  </table>

  <!-- Discord section -->
  <p style="margin:0 0 16px;font-size:16px;color:#1d1d1f;line-height:1.6;">
    È stato creato anche un canale Discord dedicato al tuo team per comunicazioni e supporto:
  </p>

  <table width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 32px;">
  <tr><td align="center">
    <a href="{invite_url}"
       style="display:inline-block;padding:14px 32px;background:#5865F2;color:#ffffff;
              font-size:15px;font-weight:600;text-decoration:none;border-radius:10px;">
      Unisciti al canale Discord
    </a>
  </td></tr>
  </table>

  <p style="margin:0;font-size:14px;color:#86868b;line-height:1.6;">
    Se hai problemi di accesso, rispondi a questa email o contatta l'amministratore.
  </p>
</td></tr>

<!-- Footer -->
<tr><td style="padding:24px 40px;border-top:1px solid #e5e5e7;text-align:center;">
  <p style="margin:0;font-size:12px;color:#86868b;">
    Nuova Accademia di Belle Arti — Perforce Admin
  </p>
</td></tr>

</table>
</td></tr>
</table>
</body>
</html>
"""


def send_email(
    to_email: str,
    subject: str,
    html_body: str,
    resend_api_key: str,
    dry_run: bool = False,
) -> bool:
    """Send an HTML email via Resend API."""
    if dry_run:
        print(f"    [dry-run] Would send email to {to_email}")
        return True

    payload = json.dumps({
        "from": RESEND_FROM,
        "to": [to_email],
        "subject": subject,
        "html": html_body,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {resend_api_key}",
            "Content-Type": "application/json",
            "User-Agent": "NABA-Perforce-Bot/1.0",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read().decode())
            print(f"    [sent] Email to {to_email} (id: {result.get('id', 'ok')})")
            return True
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        print(f"    [ERROR] Failed to send email to {to_email}: {e.code} {error_body}")
        return False
    except Exception as e:
        print(f"    [ERROR] Failed to send email to {to_email}: {e}")
        return False


# ── CSV helpers ─────────────────────────────────────────────
def read_csv(path: Path) -> list[dict]:
    with open(path, "r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ── Public API (called from perforce_provision.py) ──────────
def provision_discord_and_email(
    users: list[dict],
    discord_token: str = None,
    resend_api_key: str = None,
    category_name: str = DISCORD_CATEGORY_NAME,
    dry_run: bool = False,
):
    """
    For a list of user dicts (same team), create Discord channel + role
    and send welcome emails. Returns the invite URL or None.
    """
    if not users:
        return None

    team = users[0]["team"].strip()

    print(f"\n{'─' * 50}")
    print(f"Discord + Email for team: {team}")
    print(f"{'─' * 50}")

    invite_url = None

    # ── Discord ──
    if discord_token:
        # Find category
        category_id = find_category_id(discord_token, category_name)
        if not category_id and not dry_run:
            print(f"    [WARNING] Category '{category_name}' not found. Channel will be created without a category.")
            print(f"    Available categories will be listed for you to choose.")
            channels = get_guild_channels(discord_token)
            categories = [ch for ch in channels if ch.get("type") == 4]
            if categories:
                print(f"    Available categories:")
                for i, cat in enumerate(categories):
                    print(f"      {i + 1}. {cat['name']}")
                choice = input(f"    Enter number (or Enter to skip): ").strip()
                if choice.isdigit() and 1 <= int(choice) <= len(categories):
                    category_id = categories[int(choice) - 1]["id"]
                    print(f"    Using category: {categories[int(choice) - 1]['name']}")

        # Create role
        role = create_role(discord_token, team, dry_run)
        if not role:
            print(f"    [ERROR] Could not create role. Skipping channel.")
        else:
            role_id = role["id"]

            # Create channel
            channel = create_channel(discord_token, team, role_id, category_id, dry_run)

            if channel:
                channel_id = channel["id"]
                invite_url = create_invite(discord_token, channel_id, dry_run)

                # Send a welcome message in the channel
                if not dry_run and channel_id != "dry-run":
                    member_list = "\n".join(f"• **{u['full_name']}** (`{u['username']}`)" for u in users)
                    welcome_msg = (
                        f"## Benvenuti nel canale del team {team}!\n\n"
                        f"Questo canale è dedicato al vostro progetto. "
                        f"Usatelo per comunicazioni, domande e supporto.\n\n"
                        f"**Membri del team:**\n{member_list}\n\n"
                        f"**Depot Perforce:** `//{team}/...`\n"
                        f"**Server:** `perforce.naba.it:1666`"
                    )
                    discord_request("POST", f"/channels/{channel_id}/messages", discord_token, {
                        "content": welcome_msg
                    })
    else:
        print("    [skip] No Discord token — skipping Discord setup")

    # ── Emails ──
    if resend_api_key:
        for user in users:
            subject = EMAIL_SUBJECT_IT.format(team=team)
            html = build_email_html(user, team, invite_url or "https://discord.gg/your-server")
            send_email(user["email"].strip(), subject, html, resend_api_key, dry_run)
    else:
        print("    [skip] No Resend API key — skipping emails")

    return invite_url


# ── Standalone mode ─────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Discord + Email provisioning for Perforce users",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
This script processes users with status 'created' in the CSV.
Run it after perforce_provision.py, or use the integrated mode.

Examples:
  python discord_email_provision.py --dry-run
  python discord_email_provision.py --csv data/users.csv
  python discord_email_provision.py --category "Progetti 2026"
        """,
    )
    parser.add_argument("--csv", type=Path, default=Path("data/users.csv"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--category", type=str, default=DISCORD_CATEGORY_NAME,
                        help=f"Discord category name (default: {DISCORD_CATEGORY_NAME})")
    parser.add_argument("--skip-discord", action="store_true", help="Skip Discord setup")
    parser.add_argument("--skip-email", action="store_true", help="Skip email sending")
    args = parser.parse_args()

    if not args.csv.exists():
        print(f"ERROR: CSV not found: {args.csv}")
        sys.exit(1)

    # Interactive credential prompts
    discord_token = None
    resend_api_key = None

    if not args.skip_discord:
        discord_token = getpass.getpass("Discord bot token: ")
        if not discord_token.strip():
            print("No token entered — skipping Discord.")
            discord_token = None

    if not args.skip_email:
        resend_api_key = getpass.getpass("Resend API key: ")
        if not resend_api_key.strip():
            print("No key entered — skipping emails.")
            resend_api_key = None

    # Read CSV and group by team
    rows = read_csv(args.csv)
    # Process users that have been created in Perforce but not yet set up in Discord
    created = [r for r in rows if r.get("status", "").strip().lower() == "created"]

    if not created:
        print("No 'created' users to process. Run perforce_provision.py first.")
        return

    # Group by team
    teams = {}
    for user in created:
        team = user["team"].strip()
        if team not in teams:
            teams[team] = []
        teams[team].append(user)

    print(f"\nProcessing {len(created)} user(s) across {len(teams)} team(s):\n")

    for team, users in teams.items():
        provision_discord_and_email(
            users=users,
            discord_token=discord_token,
            resend_api_key=resend_api_key,
            category_name=args.category,
            dry_run=args.dry_run,
        )

    print(f"\n{'═' * 50}")
    print("Done!")


if __name__ == "__main__":
    main()
