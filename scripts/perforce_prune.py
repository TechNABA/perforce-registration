#!/usr/bin/env python3
"""
perforce_prune.py

Finds all Perforce users who have no access to any depot
(no group membership with protections, no direct user protections)
and removes them along with their workspaces.

This is useful for cleaning up orphaned accounts — users who were
removed from all groups but whose account still exists.

Server, user and password are asked in sequence at every run: the address
depends on the network you are working from, and from the virtual studio VLAN
the server is only reachable by IP.

Usage:
    python perforce_prune.py --dry-run          # preview who would be removed
    python perforce_prune.py                     # execute removal
    python perforce_prune.py --keep admin,villal # extra users to never remove
"""

import argparse
import getpass
import os
import subprocess
import sys


# ══════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════
# Server, user and password are asked at every run: no server address and no
# account are left in the code, and the repo is public. It is also needed
# because from the virtual studio VLAN the server is only reachable by IP.
P4PORT = ""
P4USER = ""
P4PASSWD = ""

# Users that should never be removed (service accounts, admins).
# The connecting account is added to this set at runtime.
ALWAYS_KEEP = {
    "villal",
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


def ask_p4_connection() -> None:
    """
    Ask for server, user and password, in that order, at every run.
    The address depends on the network you are working from, so it has no
    default: from the virtual studio VLAN it has to be given as an IP.
    """
    global P4PORT, P4USER, P4PASSWD

    P4PORT = input("Perforce server (host:port): ").strip()
    if not P4PORT:
        print("ERROR: the server address is required.")
        sys.exit(1)

    P4USER = input("Perforce user: ").strip()
    if not P4USER:
        print("ERROR: the user is required.")
        sys.exit(1)

    P4PASSWD = getpass.getpass(f"Password for {P4USER}: ")


def get_all_users() -> list[str]:
    """Get all Perforce usernames."""
    result = p4("users")
    users = []
    for line in result.stdout.strip().split("\n"):
        if line.strip():
            users.append(line.split(" ")[0])
    return users


def get_users_in_groups() -> set[str]:
    """Get all users who belong to at least one group."""
    result = p4("groups")
    users_with_groups = set()

    for group_name in result.stdout.strip().split("\n"):
        group_name = group_name.strip()
        if not group_name:
            continue

        spec = p4(f"group -o {group_name}")
        in_users = False
        for line in spec.stdout.split("\n"):
            if line.startswith("Users:"):
                in_users = True
                continue
            if in_users:
                if line.startswith("\t"):
                    users_with_groups.add(line.strip())
                else:
                    break

    return users_with_groups


def get_users_in_protections() -> set[str]:
    """Get all users who are directly referenced in the protections table."""
    result = p4("protect -o")
    users_with_protections = set()

    for line in result.stdout.split("\n"):
        stripped = line.strip()
        # Match lines like: write user mario_rossi * //depot/...
        parts = stripped.split()
        if len(parts) >= 4 and parts[1] == "user":
            users_with_protections.add(parts[2])

    return users_with_protections


def get_groups_in_protections() -> set[str]:
    """Get all groups referenced in the protections table."""
    result = p4("protect -o")
    groups = set()

    for line in result.stdout.split("\n"):
        stripped = line.strip()
        parts = stripped.split()
        if len(parts) >= 4 and parts[1] == "group":
            groups.add(parts[2])

    return groups


def get_user_workspaces(username: str) -> list[str]:
    result = p4(f"clients -u {username}")
    workspaces = []
    for line in result.stdout.strip().split("\n"):
        if line.startswith("Client "):
            workspaces.append(line.split(" ")[1])
    return workspaces


def delete_workspace(ws_name: str, dry_run: bool = False) -> bool:
    if dry_run:
        return True
    p4(f"-c {ws_name} revert //...")
    result = p4(f"client -d -f {ws_name}")
    return result.returncode == 0


def delete_user(username: str, dry_run: bool = False) -> bool:
    if dry_run:
        return True
    result = p4(f"user -d -f {username}")
    return result.returncode == 0


def main():
    parser = argparse.ArgumentParser(
        description="Remove Perforce users with no depot access",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
A user is considered to have "no depot access" if:
  - They are NOT a member of any group that has protections
  - They are NOT directly referenced in the protections table

Server, user and password are asked at startup, in that order.

Examples:
  python perforce_prune.py --dry-run
  python perforce_prune.py
  python perforce_prune.py --keep admin,servicebot
        """,
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without making changes")
    parser.add_argument("--keep", type=str, default="",
                        help="Comma-separated list of extra usernames to never remove")
    args = parser.parse_args()

    # Server, user, password
    ask_p4_connection()

    # Build keep list — the connecting account is never a candidate
    keep = set(ALWAYS_KEEP)
    keep.add(P4USER.lower())
    if args.keep:
        for u in args.keep.split(","):
            keep.add(u.strip().lower())

    # Connect
    print(f"\nConnecting to {P4PORT}...")
    result = p4("info")
    if result.returncode != 0:
        print(f"ERROR: Cannot connect: {result.stderr.strip()}")
        sys.exit(1)
    print("Connected!\n")

    if args.dry_run:
        print("*** DRY RUN — no changes will be made ***\n")

    # Gather data
    print("Fetching all users...")
    all_users = get_all_users()
    print(f"  Total users: {len(all_users)}")

    print("Fetching group memberships...")
    users_in_groups = get_users_in_groups()
    print(f"  Users in groups: {len(users_in_groups)}")

    print("Fetching protections table...")
    users_in_protections = get_users_in_protections()
    groups_in_protections = get_groups_in_protections()
    print(f"  Users with direct protections: {len(users_in_protections)}")
    print(f"  Groups with protections: {len(groups_in_protections)}")

    # Find users in groups that actually have protections
    users_with_effective_access = set()

    # Users directly in protections table
    users_with_effective_access.update(users_in_protections)

    # Users in groups that have protections
    result_groups = p4("groups")
    for group_name in result_groups.stdout.strip().split("\n"):
        group_name = group_name.strip()
        if not group_name:
            continue
        if group_name in groups_in_protections:
            # This group has depot access — all its members have access
            spec = p4(f"group -o {group_name}")
            in_users = False
            for line in spec.stdout.split("\n"):
                if line.startswith("Users:"):
                    in_users = True
                    continue
                if in_users:
                    if line.startswith("\t"):
                        users_with_effective_access.add(line.strip())
                    else:
                        break

    print(f"\n  Users with effective depot access: {len(users_with_effective_access)}")

    # Find orphaned users
    orphaned = []
    for user in all_users:
        if user.lower() in keep:
            continue
        if user not in users_with_effective_access:
            orphaned.append(user)

    if not orphaned:
        print("\nNo orphaned users found. All users have depot access.")
        return

    # Show orphaned users
    print(f"\n{'═' * 60}")
    print(f"ORPHANED USERS: {len(orphaned)} user(s) with no depot access")
    print(f"{'═' * 60}")
    for u in sorted(orphaned):
        ws_count = len(get_user_workspaces(u))
        ws_info = f"({ws_count} workspace{'s' if ws_count != 1 else ''})" if ws_count > 0 else ""
        print(f"  {u:<30} {ws_info}")

    print(f"\nProtected users (will NOT be removed): {', '.join(sorted(keep))}")

    # Confirm
    if not args.dry_run:
        print(f"\n⚠️  This will permanently delete {len(orphaned)} user(s) and their workspaces.")
        confirm = input("Type CONFIRM to proceed: ").strip()
        if confirm != "CONFIRM":
            print("Aborted.")
            return

    # Process removals
    removed = 0
    errors = 0

    for user in sorted(orphaned):
        print(f"\n{'─' * 40}")
        print(f"Removing: {user}")

        # Delete workspaces first
        workspaces = get_user_workspaces(user)
        for ws in workspaces:
            if delete_workspace(ws, args.dry_run):
                print(f"  [{'dry-run' if args.dry_run else 'deleted'}] Workspace '{ws}'")
            else:
                print(f"  [ERROR] Could not delete workspace '{ws}'")

        # Delete user
        if delete_user(user, args.dry_run):
            print(f"  [{'dry-run' if args.dry_run else 'deleted'}] User '{user}'")
            removed += 1
        else:
            print(f"  [ERROR] Could not delete user '{user}'")
            errors += 1

    print(f"\n{'═' * 60}")
    print(f"PRUNE COMPLETE: {removed} removed, {errors} errors")
    if args.dry_run:
        print("\n*** This was a dry run. Run again without --dry-run to apply. ***")


if __name__ == "__main__":
    main()
