#!/usr/bin/env python3
"""
perforce_prune.py

Finds all Perforce users who have no access to any depot
(no group membership with protections, no direct user protections)
and removes them along with their workspaces and pending changelists.

This is useful for cleaning up orphaned accounts — users who were
removed from all groups but whose account still exists.

Server, user and password are asked in sequence at every run: the address
depends on the network you are working from, and from the virtual studio VLAN
the server is only reachable by IP.

Usage:
    python perforce_prune.py --dry-run          # preview who would be removed
    python perforce_prune.py                     # execute removal
    python perforce_prune.py --keep admin,bot    # extra users to never remove
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import p4_common as p4c


# ══════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════
# No protected account is hardcoded here: the repo is public, and the one
# account that always has to be protected is the one you connect with, which is
# only known at runtime. Service accounts go in --keep.
#
# The connection is created in main() and passed to the p4_common functions.
P4 = None
# ══════════════════════════════════════════════════════════════


def get_all_users() -> list[str]:
    """Get all Perforce usernames."""
    result = P4.run("users")
    users = []
    for line in result.stdout.strip().split("\n"):
        if line.strip():
            users.append(line.split(" ")[0])
    return users


def get_all_groups() -> list[str]:
    result = P4.run("groups")
    return [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]


def get_users_in_groups() -> set[str]:
    """Get all users who belong to at least one group."""
    users_with_groups = set()
    for group_name in get_all_groups():
        spec = P4.run("group", "-o", group_name)
        users_with_groups.update(p4c.parse_group_members(spec.stdout))
    return users_with_groups


def get_users_in_protections() -> set[str]:
    """Get all users who are directly referenced in the protections table."""
    result = P4.run("protect", "-o")
    users_with_protections = set()

    for line in result.stdout.split("\n"):
        # Match lines like: write user mario_rossi * //depot/...
        parts = line.strip().split()
        if len(parts) >= 4 and parts[1] == "user":
            users_with_protections.add(parts[2])

    return users_with_protections


def get_groups_in_protections() -> set[str]:
    """Get all groups referenced in the protections table."""
    result = P4.run("protect", "-o")
    groups = set()

    for line in result.stdout.split("\n"):
        parts = line.strip().split()
        if len(parts) >= 4 and parts[1] == "group":
            groups.add(parts[2])

    return groups


def main():
    parser = argparse.ArgumentParser(
        description="Remove Perforce users with no depot access",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
A user is considered to have "no depot access" if:
  - They are NOT a member of any group that has protections
  - They are NOT directly referenced in the protections table

The account you connect with is always protected. Any other account that must
never be removed — service accounts, other admins — has to be listed in --keep:
nothing is hardcoded, because this repo is public.

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
    global P4
    P4 = p4c.ask_p4_connection()

    # Build keep list — the connecting account is never a candidate
    keep = {P4.user.lower()}
    if args.keep:
        for u in args.keep.split(","):
            if u.strip():
                keep.add(u.strip().lower())

    # Connect
    print(f"\nConnecting to {P4.port}...")
    result = p4c.connect(P4)
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
    for group_name in get_all_groups():
        if group_name in groups_in_protections:
            # This group has depot access — all its members have access
            spec = P4.run("group", "-o", group_name)
            users_with_effective_access.update(p4c.parse_group_members(spec.stdout))

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
        ws_count = len(p4c.user_workspaces(P4, u))
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
    tag = "dry-run" if args.dry_run else None

    for user in sorted(orphaned):
        print(f"\n{'─' * 40}")
        print(f"Removing: {user}")

        user_errors = 0

        # Pending changelists first: they keep files open and block the account
        for change in p4c.pending_changes(P4, user):
            ok, err = p4c.delete_pending_change(P4, change, args.dry_run)
            if ok:
                print(f"  [{tag or 'deleted'}] Changelist {change}")
            else:
                print(f"  [ERROR] Could not delete changelist {change}: {err}")
                errors += 1
                user_errors += 1

        # Then workspaces
        for ws in p4c.user_workspaces(P4, user):
            ok, err = p4c.delete_workspace(P4, ws, args.dry_run)
            if ok:
                print(f"  [{tag or 'deleted'}] Workspace '{ws}'")
            else:
                print(f"  [ERROR] Could not delete workspace '{ws}': {err}")
                errors += 1
                user_errors += 1

        # `user -d -f` removes the account but leaves any undeleted workspace or
        # changelist behind: this script iterates `p4 users`, so once the account
        # is gone the orphan is never seen again.
        if user_errors:
            print(f"  [skip] User '{user}' kept: {user_errors} object(s) above could not be removed")
            continue

        # And finally the account
        ok, err = p4c.delete_user(P4, user, args.dry_run)
        if ok:
            print(f"  [{tag or 'deleted'}] User '{user}'")
            removed += 1
        else:
            print(f"  [ERROR] Could not delete user '{user}': {err}")
            errors += 1

    print(f"\n{'═' * 60}")
    print(f"PRUNE COMPLETE: {removed} removed, {errors} errors")
    if args.dry_run:
        print("\n*** This was a dry run. Run again without --dry-run to apply. ***")


if __name__ == "__main__":
    main()
