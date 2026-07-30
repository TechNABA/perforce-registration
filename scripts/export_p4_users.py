#!/usr/bin/env python3
"""
export_p4_users.py

Esporta gli utenti Perforce già esistenti verso il KV Cloudflare, via
l'endpoint /import del Worker. Vengono marcati con status 'existing',
così perforce_provision.py non prova mai a ricrearli.

Lo script:
  1. Si connette a Perforce e scarica tutti gli utenti (p4 users)
  2. Per ognuno legge lo spec completo (p4 user -o)
  3. Legge i gruppi e mappa utente → gruppo (= team)
  4. Manda tutto al Worker

Uso:
    python export_p4_users.py                    # invia al Worker
    python export_p4_users.py --skip-existing    # non sovrascrive i record già nel KV
    python export_p4_users.py --dry-run          # anteprima senza inviare nulla
    python export_p4_users.py --csv out.csv      # salva anche un CSV locale (dati personali)
"""

import argparse
import csv
import getpass
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import naba_store
from naba_store import FIELDS, StoreError


# ══════════════════════════════════════════════════════════════
# CONFIGURAZIONE
# ══════════════════════════════════════════════════════════════
P4PORT = "perforce.naba.it:1666"
P4USER = "villal"
P4PASSWD = ""

# Utenti esclusi dall'export (account di servizio, admin, ecc.)
EXCLUDE_USERS = {
    "villal",       # account admin — aggiungerne altri qui se serve
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
    """Legge tutti gli utenti con p4 users e il loro spec completo."""
    result = p4("users")
    if result.returncode != 0:
        print(f"ERRORE: p4 users fallito: {result.stderr.strip()}")
        sys.exit(1)

    users = []
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        # Formato: "username <email> (Full Name) accessed YYYY/MM/DD"
        username = line.split(" ")[0]

        if username in EXCLUDE_USERS:
            continue

        # Lo spec completo è più affidabile del parsing della riga
        spec_result = p4(f"user -o {username}")
        if spec_result.returncode != 0:
            continue

        user_data = {"username": username, "full_name": "", "email": ""}

        for spec_line in spec_result.stdout.split("\n"):
            if spec_line.startswith("FullName:"):
                user_data["full_name"] = spec_line.split("\t", 1)[-1].strip()
            elif spec_line.startswith("Email:"):
                user_data["email"] = spec_line.split("\t", 1)[-1].strip()

        users.append(user_data)

    return users


def get_user_groups() -> dict[str, list[str]]:
    """Legge tutti i gruppi e ritorna la mappa username → lista di gruppi."""
    result = p4("groups")
    if result.returncode != 0:
        print(f"ATTENZIONE: p4 groups fallito: {result.stderr.strip()}")
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
                        user_groups.setdefault(member, []).append(group_name)
                else:
                    in_users = False

    return user_groups


def main():
    parser = argparse.ArgumentParser(
        description="Esporta gli utenti Perforce esistenti verso il KV Cloudflare",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--skip-existing", action="store_true",
                        help="Non sovrascrive i record già presenti nel KV")
    parser.add_argument("--dry-run", action="store_true",
                        help="Anteprima senza inviare nulla al Worker")
    parser.add_argument("--csv", type=Path, default=None,
                        help="Salva anche un CSV locale (contiene dati personali)")
    args = parser.parse_args()

    # Password Perforce
    global P4PASSWD
    print(f"Server: {P4PORT}")
    print(f"Utente: {P4USER}")
    P4PASSWD = getpass.getpass(f"Password per {P4USER}: ")

    print(f"\nConnessione a {P4PORT}...")
    result = p4("info")
    if result.returncode != 0:
        print(f"ERRORE: connessione fallita: {result.stderr.strip()}")
        sys.exit(1)
    print("Connesso.\n")

    print("Lettura utenti...")
    users = get_all_users()
    print(f"  {len(users)} utenti (esclusi gli account di servizio)\n")

    print("Lettura gruppi...")
    user_groups = get_user_groups()
    groups_found = set()
    for groups in user_groups.values():
        groups_found.update(groups)
    print(f"  {len(groups_found)} gruppi\n")

    # Un record per coppia utente/gruppo: chi sta in più gruppi ha più record.
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    rows = []
    multi_group_users = []

    for user in users:
        username = user["username"]
        groups = user_groups.get(username, []) or ["Unassigned"]

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

    rows.sort(key=lambda r: (r["team"].lower(), r["username"].lower()))

    # Anteprima senza nomi completi né email.
    print(f"{'─' * 60}")
    print(f"{'Username':<30} Team")
    print(f"{'─' * 60}")
    for r in rows:
        print(f"{r['username']:<30} {r['team']}")
    print(f"{'─' * 60}")
    print(f"Totale: {len(rows)} record ({len(users)} utenti unici)\n")

    if multi_group_users:
        print(f"Utenti in più team ({len(multi_group_users)}):")
        for username, groups in multi_group_users:
            print(f"  {username} → {', '.join(groups)}")
        print()

    teams = {}
    for r in rows:
        teams[r["team"]] = teams.get(r["team"], 0) + 1
    print("Team:")
    for team_name, count in sorted(teams.items()):
        print(f"  {team_name}: {count} membri")
    print()

    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        print(f"CSV locale scritto: {args.csv}")
        print("Contiene dati personali: non committarlo.\n")

    if args.dry_run:
        print("*** Dry run — niente inviato al Worker ***")
        return

    # Invio al Worker
    print(f"Worker: {naba_store.worker_url()}")
    token = naba_store.get_admin_token()

    try:
        before = len(naba_store.fetch_users(token))
        print(f"Connesso. Record già nel KV: {before}\n")

        print(f"Invio di {len(rows)} record...")
        result = naba_store.import_users(
            token, rows, skip_existing=args.skip_existing, default_status="existing"
        )
    except StoreError as e:
        print(f"ERRORE: {e}")
        sys.exit(1)

    print(f"  scritti: {result['written']}")
    if result["skipped"]:
        print(f"  saltati (già presenti): {result['skipped']}")

    if result["rejected"]:
        print(f"\n{len(result['rejected'])} record rifiutati dal Worker:")
        for r in result["rejected"]:
            print(f"  ! {r.get('username', '?')}: {r.get('error', 'errore sconosciuto')}")

    print("\nTutti gli utenti hanno status 'existing' — il provisioning li salterà.")


if __name__ == "__main__":
    main()
