#!/usr/bin/env python3
"""
export_p4_users.py

Exports all existing Perforce users into data/users.csv format.
Users are marked with status 'existing' so perforce_provision.py
will never try to recreate them.

The script:
  1. Connects to Perforce and fetches all users (p4 users)
  2. For each user, fetches their full spec (p4 user -o)
  3. Fetches all groups and maps users to their group (= team)
  4. Writes everything to the CSV

Usage:
    python export_p4_users.py                          # writes to data/users.csv
    python export_p4_users.py --output my_export.csv   # custom output path
    python export_p4_users.py --merge                  # merge with existing CSV (no duplicates)
    python export_p4_users.py --dry-run                # preview without writing
"""

import argparse
import csv
import getpass
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path


# ══════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════
P4PORT = "10.150.3.1:1666"
P4USER = "villal"
P4PASSWD = ""

FIELDS = [
    "timestamp", "username", "full_name", "email",
    "team", "tesista", "anno_corso", "status",
]

# Users to exclude from export (service accounts, admin accounts, etc.)
EXCLUDE_USERS = {
    "villal",       # admin account — add others here if needed
}
# ══════════════════════════════════════════════════════════════


def get_p4_env() -> dict:
    env = os.environ.copy()
    env["P4PORT"] = P4PORT
    env["P4USER"] = P4USER
    if P4PASSWD:
        env["P4PASSWD"] = P4PASSWD
    return env


def p4(cmd: str, stdin_text: str = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        f"p4 {cmd}",
        shell=True,
        capture_output=True,
        text=True,
        input=stdin_text,
        env=get_p4_env(),
    )


def get_all_users() -> list[dict]:
    """Fetch all users with p4 users and their full specs."""
    result = p4("users")
    if result.returncode != 0:
        print(f"ERROR: p4 users failed: {result.stderr.strip()}")
        sys.exit(1)

    users = []
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        # Format: "username <email> (Full Name) accessed YYYY/MM/DD"
        parts = line.split(" ")
        username = parts[0]

        if username in EXCLUDE_USERS:
            continue

        # Get full user spec for reliable data
        spec_result = p4(f"user -o {username}")
        if spec_result.returncode != 0:
            continue

        user_data = {
            "username": username,
            "full_name": "",
            "email": "",
        }

        for spec_line in spec_result.stdout.split("\n"):
            if spec_line.startswith("FullName:"):
                user_data["full_name"] = spec_line.split("\t", 1)[-1].strip()
            elif spec_line.startswith("Email:"):
                user_data["email"] = spec_line.split("\t", 1)[-1].strip()

        users.append(user_data)

    return users


def get_user_groups() -> dict[str, list[str]]:
    """Fetch all groups and return a mapping of username → list of groups."""
    result = p4("groups")
    if result.returncode != 0:
        print(f"WARNING: p4 groups failed: {result.stderr.strip()}")
        return {}

    user_groups = {}

    for group_name in result.stdout.strip().split("\n"):
        group_name = group_name.strip()
        if not group_name:
            continue

        spec_result = p4(f"group -o {group_name}")
        if spec_result.returncode != 0:
            continue

        in_users = False
        for line in spec_result.stdout.split("\n"):
            if line.startswith("Users:"):
                in_users = True
                continue
            if in_users:
                if line.startswith("\t"):
                    member = line.strip()
                    if member not in EXCLUDE_USERS:
                        if member not in user_groups:
                            user_groups[member] = []
                        user_groups[member].append(group_name)
                else:
                    in_users = False

    return user_groups


def read_existing_csv(path: Path) -> set[str]:
    """Read existing CSV and return set of usernames."""
    if not path.exists():
        return set()
    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return {row["username"].strip().lower() for row in reader}


def read_existing_rows(path: Path) -> list[dict]:
    """Read existing CSV rows."""
    if not path.exists():
        return []
    with open(path, "r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main():
    parser = argparse.ArgumentParser(
        description="Export existing Perforce users to CSV",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--output", type=Path, default=Path("data/users.csv"),
                        help="Output CSV path (default: data/users.csv)")
    parser.add_argument("--merge", action="store_true",
                        help="Merge with existing CSV instead of overwriting")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without writing any files")
    args = parser.parse_args()

    # Ask for password
    global P4PASSWD
    print(f"Server: {P4PORT}")
    print(f"User:   {P4USER}")
    P4PASSWD = getpass.getpass(f"Password for {P4USER}: ")

    # Test connection
    print(f"\nConnecting to {P4PORT}...")
    result = p4("info")
    if result.returncode != 0:
        print(f"ERROR: Cannot connect: {result.stderr.strip()}")
        sys.exit(1)
    print("Connected!\n")

    # Fetch users
    print("Fetching users...")
    users = get_all_users()
    print(f"  Found {len(users)} users (excluding service accounts)\n")

    # Fetch groups
    print("Fetching groups...")
    user_groups = get_user_groups()
    groups_found = set()
    for groups in user_groups.values():
        groups_found.update(groups)
    print(f"  Found {len(groups_found)} groups\n")

    # Build CSV rows — one row per user-group combination
    # If a user belongs to multiple groups, they get multiple rows
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
    rows = []
    multi_group_users = []

    for user in users:
        username = user["username"]
        groups = user_groups.get(username, [])

        if not groups:
            groups = ["Unassigned"]

        if len(groups) > 1:
            multi_group_users.append((username, groups))

        for group in groups:
            rows.append({
                "timestamp": now,
                "username": username,
                "full_name": user["full_name"] or username,
                "email": user["email"] or f"{username}@studenti.naba.it",
                "team": group,
                "tesista": "",
                "anno_corso": "",
                "status": "existing",
            })

    # Sort by team then name
    rows.sort(key=lambda r: (r["team"].lower(), r["full_name"].lower()))

    # Preview
    print(f"{'─' * 70}")
    print(f"{'Username':<25} {'Team':<25} {'Email'}")
    print(f"{'─' * 70}")
    for r in rows:
        print(f"{r['username']:<25} {r['team']:<25} {r['email']}")
    print(f"{'─' * 70}")
    print(f"Total: {len(rows)} rows ({len(users)} unique users)\n")

    # Show multi-group users
    if multi_group_users:
        print(f"Users in multiple teams ({len(multi_group_users)}):")
        for username, groups in multi_group_users:
            print(f"  {username} → {', '.join(groups)}")
        print()

    # Group summary
    teams = {}
    for r in rows:
        t = r["team"]
        teams[t] = teams.get(t, 0) + 1
    print("Teams:")
    for t, count in sorted(teams.items()):
        print(f"  {t}: {count} members")
    print()

    if args.dry_run:
        print("*** Dry run — no files written ***")
        return

    # Merge or overwrite
    if args.merge:
        existing_rows = read_existing_rows(args.output)
        # Deduplicate by username+team pair (not just username, since users can be in multiple teams)
        existing_pairs = {
            (r["username"].strip().lower(), r.get("team", "").strip().lower())
            for r in existing_rows
        }
        new_count = 0
        for r in rows:
            pair = (r["username"].strip().lower(), r["team"].strip().lower())
            if pair not in existing_pairs:
                existing_rows.append(r)
                new_count += 1
        existing_rows.sort(key=lambda r: (r.get("team", "").lower(), r.get("full_name", "").lower()))
        final_rows = existing_rows
        print(f"Merging: {new_count} new rows added, {len(existing_pairs)} already in CSV")
    else:
        final_rows = rows

    # Write
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(final_rows)

    print(f"\nCSV written: {args.output} ({len(final_rows)} rows)")
    print("All users have status 'existing' — provision script will skip them.")


if __name__ == "__main__":
    main()
