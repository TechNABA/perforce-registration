#!/usr/bin/env python3
"""
perforce_provision.py

Reads data/users.csv (or a custom path), and for each user with status 'pending':
  1. Creates the Perforce user
  2. Creates the group (named after the team) if it doesn't exist
  3. Adds the user to the group
  4. Creates a local depot (named after the team) if it doesn't exist
  5. Adds write protection for the group on the depot
  6. Updates the user's status to 'created' in the CSV

Run from your local machine where p4 CLI is configured and you have admin access.

Usage:
    python perforce_provision.py                      # uses data/users.csv
    python perforce_provision.py --csv path/to/file   # custom CSV path
    python perforce_provision.py --dry-run             # preview without changes
    python perforce_provision.py --password changeme   # set initial password for new users
"""

import argparse
import csv
import subprocess
import sys
import tempfile
from pathlib import Path


# ── Config ──────────────────────────────────────────────────────
DEFAULT_CSV = Path("data/users.csv")

FIELDS = [
    "timestamp", "username", "full_name", "email",
    "team", "tesista", "anno_corso", "status",
]


# ── p4 helpers ──────────────────────────────────────────────────
def p4(cmd: str, stdin_text: str = None, check: bool = True) -> subprocess.CompletedProcess:
    """Run a p4 command. Returns CompletedProcess."""
    full_cmd = f"p4 {cmd}"
    result = subprocess.run(
        full_cmd,
        shell=True,
        capture_output=True,
        text=True,
        input=stdin_text,
    )
    if check and result.returncode != 0:
        # Some commands return non-zero for "already exists" — we handle that
        pass
    return result


def p4_user_exists(username: str) -> bool:
    """Check if a Perforce user already exists."""
    result = p4(f"users {username}", check=False)
    return username in result.stdout


def p4_group_exists(group_name: str) -> bool:
    """Check if a Perforce group already exists."""
    result = p4(f"group -o {group_name}", check=False)
    # If group doesn't exist, p4 group -o still returns a template
    # but with no Users listed. Check the actual groups list instead.
    result = p4("groups", check=False)
    return group_name in result.stdout.split()


def p4_depot_exists(depot_name: str) -> bool:
    """Check if a Perforce depot already exists."""
    result = p4("depots", check=False)
    for line in result.stdout.strip().split("\n"):
        if line.startswith(f"Depot {depot_name} "):
            return True
    return False


def create_user(username: str, full_name: str, email: str, password: str = None, dry_run: bool = False) -> bool:
    """Create a Perforce user using p4 user -f -i."""
    if p4_user_exists(username):
        print(f"    [skip] User '{username}' already exists")
        return True

    # Build the user spec
    spec = (
        f"User:\t{username}\n"
        f"Email:\t{email}\n"
        f"FullName:\t{full_name}\n"
    )

    if dry_run:
        print(f"    [dry-run] Would create user '{username}' ({full_name}, {email})")
        return True

    result = p4("user -f -i", stdin_text=spec)
    if result.returncode != 0:
        print(f"    [ERROR] Failed to create user '{username}': {result.stderr.strip()}")
        return False

    print(f"    [created] User '{username}'")

    # Set password if specified
    if password:
        result = p4(f"-u {username} passwd", stdin_text=f"{password}\n{password}\n")
        if result.returncode == 0:
            print(f"    [password] Set for '{username}'")
        else:
            print(f"    [WARNING] Could not set password for '{username}': {result.stderr.strip()}")

    return True


def create_group(group_name: str, dry_run: bool = False) -> bool:
    """Create a Perforce group if it doesn't exist."""
    if p4_group_exists(group_name):
        print(f"    [skip] Group '{group_name}' already exists")
        return True

    spec = (
        f"Group:\t{group_name}\n"
        f"MaxResults:\tunset\n"
        f"MaxScanRows:\tunset\n"
        f"MaxLockTime:\tunset\n"
        f"Timeout:\t43200\n"
        f"Users:\n"
    )

    if dry_run:
        print(f"    [dry-run] Would create group '{group_name}'")
        return True

    result = p4("group -i", stdin_text=spec)
    if result.returncode != 0:
        print(f"    [ERROR] Failed to create group '{group_name}': {result.stderr.strip()}")
        return False

    print(f"    [created] Group '{group_name}'")
    return True


def add_user_to_group(username: str, group_name: str, dry_run: bool = False) -> bool:
    """Add a user to an existing Perforce group."""
    # Get current group spec
    result = p4(f"group -o {group_name}", check=False)
    if result.returncode != 0:
        print(f"    [ERROR] Cannot read group '{group_name}': {result.stderr.strip()}")
        return False

    spec_lines = result.stdout.strip().split("\n")

    # Check if user is already in the group
    in_users_section = False
    user_already_added = False
    for line in spec_lines:
        if line.startswith("Users:"):
            in_users_section = True
            continue
        if in_users_section:
            if line.startswith("\t"):
                if line.strip() == username:
                    user_already_added = True
                    break
            else:
                break

    if user_already_added:
        print(f"    [skip] User '{username}' already in group '{group_name}'")
        return True

    # Add user to the Users section
    new_spec_lines = []
    users_section_found = False
    for line in spec_lines:
        new_spec_lines.append(line)
        if line.startswith("Users:"):
            users_section_found = True
            new_spec_lines.append(f"\t{username}")

    if not users_section_found:
        new_spec_lines.append("Users:")
        new_spec_lines.append(f"\t{username}")

    new_spec = "\n".join(new_spec_lines) + "\n"

    if dry_run:
        print(f"    [dry-run] Would add '{username}' to group '{group_name}'")
        return True

    result = p4("group -i", stdin_text=new_spec)
    if result.returncode != 0:
        print(f"    [ERROR] Failed to add '{username}' to group '{group_name}': {result.stderr.strip()}")
        return False

    print(f"    [added] User '{username}' → group '{group_name}'")
    return True


def create_depot(depot_name: str, dry_run: bool = False) -> bool:
    """Create a local depot if it doesn't exist."""
    if p4_depot_exists(depot_name):
        print(f"    [skip] Depot '{depot_name}' already exists")
        return True

    spec = (
        f"Depot:\t{depot_name}\n"
        f"Type:\tlocal\n"
        f"Map:\t{depot_name}/...\n"
    )

    if dry_run:
        print(f"    [dry-run] Would create depot '{depot_name}'")
        return True

    result = p4("depot -i", stdin_text=spec)
    if result.returncode != 0:
        print(f"    [ERROR] Failed to create depot '{depot_name}': {result.stderr.strip()}")
        return False

    print(f"    [created] Depot '//{depot_name}/...'")
    return True


def add_protection(group_name: str, depot_name: str, dry_run: bool = False) -> bool:
    """Add write protection for a group on a depot, if not already present."""
    result = p4("protect -o", check=False)
    if result.returncode != 0:
        print(f"    [ERROR] Cannot read protections: {result.stderr.strip()}")
        return False

    protect_spec = result.stdout

    # The protection line we want to add
    prot_line = f"\twrite group {group_name} * //{depot_name}/..."

    # Check if it already exists
    if prot_line.strip() in protect_spec:
        print(f"    [skip] Protection already exists for group '{group_name}' on '//{depot_name}/...'")
        return True

    if dry_run:
        print(f"    [dry-run] Would add write protection: group '{group_name}' → '//{depot_name}/...'")
        return True

    # Append the new protection line before the final empty line
    # p4 protect spec ends with the Protections: section
    new_spec = protect_spec.rstrip() + "\n" + prot_line + "\n"

    result = p4("protect -i", stdin_text=new_spec)
    if result.returncode != 0:
        print(f"    [ERROR] Failed to update protections: {result.stderr.strip()}")
        return False

    print(f"    [protect] write group:{group_name} → //{depot_name}/...")
    return True


# ── CSV helpers ─────────────────────────────────────────────────
def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        print(f"ERROR: CSV not found: {path}")
        sys.exit(1)
    with open(path, "r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict]):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


# ── Main ────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Provision Perforce users from CSV")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="Path to users.csv")
    parser.add_argument("--dry-run", action="store_true", help="Preview actions without making changes")
    parser.add_argument("--password", type=str, default=None, help="Initial password for new users")
    args = parser.parse_args()

    # Verify p4 is available
    result = p4("info", check=False)
    if result.returncode != 0:
        print("ERROR: Cannot connect to Perforce server. Check your P4PORT, P4USER, P4PASSWD.")
        print(f"  p4 info output: {result.stderr.strip()}")
        sys.exit(1)

    print(f"Connected to Perforce server")
    # Extract server info
    for line in result.stdout.split("\n"):
        if "Server address" in line or "User name" in line:
            print(f"  {line.strip()}")

    if args.dry_run:
        print("\n*** DRY RUN — no changes will be made ***\n")

    # Read CSV
    rows = read_csv(args.csv)
    pending = [r for r in rows if r.get("status", "").strip().lower() == "pending"]

    if not pending:
        print("\nNo pending users to provision.")
        return

    print(f"\nFound {len(pending)} pending user(s) to provision:\n")

    # Collect unique teams to process
    teams_processed = set()
    success_count = 0
    error_count = 0

    for user in pending:
        username = user["username"].strip()
        full_name = user["full_name"].strip()
        email = user["email"].strip()
        team = user["team"].strip()

        print(f"{'─' * 50}")
        print(f"Processing: {username} ({full_name})")
        print(f"  Team: {team} | Tesista: {user.get('tesista', 'no')} | Anno: {user.get('anno_corso', '—') or '—'}")

        all_ok = True

        # 1. Create user
        if not create_user(username, full_name, email, args.password, args.dry_run):
            all_ok = False

        # 2. Create group (if first time we see this team)
        if team not in teams_processed:
            if not create_group(team, args.dry_run):
                all_ok = False

            # 4. Create depot
            if not create_depot(team, args.dry_run):
                all_ok = False

            # 5. Add protection
            if not add_protection(team, team, args.dry_run):
                all_ok = False

            teams_processed.add(team)

        # 3. Add user to group
        if not add_user_to_group(username, team, args.dry_run):
            all_ok = False

        # 6. Update status
        if all_ok and not args.dry_run:
            user["status"] = "created"
            success_count += 1
        elif not all_ok:
            user["status"] = "error"
            error_count += 1
        else:
            success_count += 1

    # Write updated CSV
    if not args.dry_run:
        write_csv(args.csv, rows)
        print(f"\n{'═' * 50}")
        print(f"CSV updated: {args.csv}")

    print(f"\n{'═' * 50}")
    print(f"DONE: {success_count} succeeded, {error_count} errors")

    if args.dry_run:
        print("\n*** This was a dry run. Run again without --dry-run to apply. ***")


if __name__ == "__main__":
    main()
