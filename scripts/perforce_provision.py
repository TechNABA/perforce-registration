#!/usr/bin/env python3
"""
perforce_provision.py

Scarica gli utenti dal Worker Cloudflare e, per ognuno con status 'pending':
  1. Crea l'utente Perforce
  2. Crea il gruppo (col nome del team) se non esiste
  3. Aggiunge l'utente al gruppo
  4. Crea un depot locale (col nome del team) se non esiste
  5. Aggiunge la protezione write per il gruppo sul depot
  6. Aggiorna lo status a 'created' sul Worker

Password Perforce e token admin vengono chiesti a runtime (input nascosto,
non salvati da nessuna parte).

Uso:
    python perforce_provision.py                            # provisioning
    python perforce_provision.py --dry-run                  # anteprima
    python perforce_provision.py --password changeme        # password iniziale
    python perforce_provision.py --export-xlsx utenti.xlsx  # XLSX in locale
"""

import argparse
import getpass
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import naba_store
from naba_store import StoreError


# ══════════════════════════════════════════════════════════════
# CONFIGURAZIONE
# ══════════════════════════════════════════════════════════════
P4PORT = "perforce.naba.it:1666"
P4USER = "villal"
P4PASSWD = ""  # impostata a runtime dal prompt
# ══════════════════════════════════════════════════════════════


# ── helper p4 ───────────────────────────────────────────────────
def get_p4_env() -> dict:
    """Environment con le impostazioni Perforce."""
    env = os.environ.copy()
    env["P4PORT"] = P4PORT
    env["P4USER"] = P4USER
    if P4PASSWD:
        env["P4PASSWD"] = P4PASSWD
    return env


def p4(cmd: str, stdin_text: str = None) -> subprocess.CompletedProcess:
    """Esegue un comando p4 con server/utente/password configurati."""
    return subprocess.run(
        f"p4 {cmd}",
        shell=True,
        capture_output=True,
        text=True,
        input=stdin_text,
        env=get_p4_env(),
    )


def p4_user_exists(username: str) -> bool:
    return username in p4(f"users {username}").stdout


def p4_group_exists(group_name: str) -> bool:
    return group_name in p4("groups").stdout.split()


def p4_depot_exists(depot_name: str) -> bool:
    for line in p4("depots").stdout.strip().split("\n"):
        if line.startswith(f"Depot {depot_name} "):
            return True
    return False


def create_user(username: str, full_name: str, email: str, password: str = None, dry_run: bool = False) -> bool:
    if p4_user_exists(username):
        print(f"    [skip] Utente '{username}' già esistente")
        return True

    spec = (
        f"User:\t{username}\n"
        f"Email:\t{email}\n"
        f"FullName:\t{full_name}\n"
    )

    if dry_run:
        print(f"    [dry-run] Creerebbe l'utente '{username}'")
        return True

    result = p4("user -f -i", stdin_text=spec)
    if result.returncode != 0:
        print(f"    [ERRORE] Creazione utente '{username}' fallita: {result.stderr.strip()}")
        return False

    print(f"    [creato] Utente '{username}'")

    if password:
        result = p4(f"-u {username} passwd", stdin_text=f"{password}\n{password}\n")
        if result.returncode == 0:
            print(f"    [password] Impostata per '{username}'")
        else:
            print(f"    [ATTENZIONE] Password non impostata per '{username}': {result.stderr.strip()}")

    return True


def create_group(group_name: str, dry_run: bool = False) -> bool:
    if p4_group_exists(group_name):
        print(f"    [skip] Gruppo '{group_name}' già esistente")
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
        print(f"    [dry-run] Creerebbe il gruppo '{group_name}'")
        return True

    result = p4("group -i", stdin_text=spec)
    if result.returncode != 0:
        print(f"    [ERRORE] Creazione gruppo '{group_name}' fallita: {result.stderr.strip()}")
        return False

    print(f"    [creato] Gruppo '{group_name}'")
    return True


def add_user_to_group(username: str, group_name: str, dry_run: bool = False) -> bool:
    result = p4(f"group -o {group_name}")
    if result.returncode != 0:
        print(f"    [ERRORE] Gruppo '{group_name}' non leggibile: {result.stderr.strip()}")
        return False

    spec_lines = result.stdout.strip().split("\n")

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
        print(f"    [skip] Utente '{username}' già nel gruppo '{group_name}'")
        return True

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
        print(f"    [dry-run] Aggiungerebbe '{username}' al gruppo '{group_name}'")
        return True

    result = p4("group -i", stdin_text=new_spec)
    if result.returncode != 0:
        print(f"    [ERRORE] '{username}' non aggiunto al gruppo '{group_name}': {result.stderr.strip()}")
        return False

    print(f"    [aggiunto] '{username}' → gruppo '{group_name}'")
    return True


def create_depot(depot_name: str, dry_run: bool = False) -> bool:
    if p4_depot_exists(depot_name):
        print(f"    [skip] Depot '{depot_name}' già esistente")
        return True

    spec = (
        f"Depot:\t{depot_name}\n"
        f"Type:\tlocal\n"
        f"Map:\t{depot_name}/...\n"
    )

    if dry_run:
        print(f"    [dry-run] Creerebbe il depot '{depot_name}'")
        return True

    result = p4("depot -i", stdin_text=spec)
    if result.returncode != 0:
        print(f"    [ERRORE] Creazione depot '{depot_name}' fallita: {result.stderr.strip()}")
        return False

    print(f"    [creato] Depot '//{depot_name}/...'")
    return True


def add_protection(group_name: str, depot_name: str, dry_run: bool = False) -> bool:
    result = p4("protect -o")
    if result.returncode != 0:
        print(f"    [ERRORE] Protezioni non leggibili: {result.stderr.strip()}")
        return False

    protect_spec = result.stdout
    prot_line = f"\twrite group {group_name} * //{depot_name}/..."

    if prot_line.strip() in protect_spec:
        print(f"    [skip] Protezione già presente per '{group_name}' su '//{depot_name}/...'")
        return True

    if dry_run:
        print(f"    [dry-run] Aggiungerebbe write: gruppo '{group_name}' → '//{depot_name}/...'")
        return True

    new_spec = protect_spec.rstrip() + "\n" + prot_line + "\n"

    result = p4("protect -i", stdin_text=new_spec)
    if result.returncode != 0:
        print(f"    [ERRORE] Aggiornamento protezioni fallito: {result.stderr.strip()}")
        return False

    print(f"    [protect] write group:{group_name} → //{depot_name}/...")
    return True


def export_xlsx(rows: list[dict], path: Path) -> None:
    """Scrive l'XLSX in locale. Il file contiene dati personali."""
    try:
        from xlsx_export import write_xlsx
    except ImportError as e:
        print(f"\n[ATTENZIONE] Export XLSX non disponibile: {e}")
        print("Serve openpyxl: pip install -r requirements.txt")
        return

    write_xlsx(rows, path)
    print(f"\nXLSX scritto: {path} ({len(rows)} utenti)")
    print("Contiene dati personali: non committarlo.")


# ── Main ────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Provisioning utenti Perforce dai dati sul Worker Cloudflare",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Esempi:
  python perforce_provision.py --dry-run
  python perforce_provision.py
  python perforce_provision.py --password Welcome2026!
  python perforce_provision.py --export-xlsx ~/Desktop/utenti.xlsx
        """,
    )
    parser.add_argument("--dry-run", action="store_true", help="Anteprima senza modifiche")
    parser.add_argument("--password", type=str, default=None,
                        help="Password iniziale per i nuovi utenti Perforce")
    parser.add_argument("--skip-discord", action="store_true", help="Salta creazione ruoli/canali Discord")
    parser.add_argument("--skip-email", action="store_true", help="Salta invio email di benvenuto")
    parser.add_argument("--category", type=str, default="Tesi",
                        help="Categoria Discord per i nuovi canali (default: Tesi)")
    parser.add_argument("--export-xlsx", type=Path, default=None,
                        help="Genera l'XLSX degli utenti in locale a fine esecuzione")
    args = parser.parse_args()

    # ── Dati dal Worker ──
    print(f"Worker: {naba_store.worker_url()}")
    admin_token = naba_store.get_admin_token()

    try:
        rows = naba_store.fetch_users(admin_token)
    except StoreError as e:
        print(f"ERRORE: {e}")
        sys.exit(1)

    print(f"Scaricati {len(rows)} record dal KV")

    # ── Password Perforce ──
    global P4PASSWD
    print(f"\nServer: {P4PORT}")
    print(f"Utente: {P4USER}")
    P4PASSWD = getpass.getpass(f"Password per {P4USER}: ")

    print(f"Connessione a {P4PORT}...")
    result = p4("info")
    if result.returncode != 0:
        print("ERRORE: connessione al server Perforce fallita.")
        print(f"  Server: {P4PORT}")
        print(f"  Utente: {P4USER}")
        print(f"  Errore: {result.stderr.strip()}")
        print()
        print("Verifica che:")
        print("  1. il client p4 sia installato e nel PATH")
        print("  2. il server sia raggiungibile dalla rete")
        print("  3. la password sia corretta")
        sys.exit(1)

    print("Connesso al server Perforce")
    for line in result.stdout.split("\n"):
        if any(k in line for k in ["Server address", "User name", "Server version"]):
            print(f"  {line.strip()}")

    if args.dry_run:
        print("\n*** DRY RUN — nessuna modifica verrà applicata ***\n")

    pending = [r for r in rows if r.get("status", "").strip().lower() == "pending"]

    if not pending:
        print("\nNessun utente pending da processare.")
        if args.export_xlsx:
            export_xlsx(rows, args.export_xlsx)
        return

    print(f"\n{len(pending)} utente/i da processare:\n")

    teams_processed = set()
    status_updates = []
    success_count = 0
    error_count = 0

    for user in pending:
        username = user["username"].strip()
        full_name = user["full_name"].strip()
        email = user["email"].strip()
        team = user["team"].strip()

        print(f"{'─' * 50}")
        # Nome completo ed email non vanno a schermo: restano solo nel record.
        print(f"Elaborazione: {username}")
        print(f"  Team: {team} | Tesista: {user.get('tesista', 'no')} | Anno: {user.get('anno_corso', '') or '—'}")

        all_ok = True

        # 1. Utente
        if not create_user(username, full_name, email, args.password, args.dry_run):
            all_ok = False

        # 2. Gruppo + depot + protezione (una volta per team)
        if team not in teams_processed:
            if not create_group(team, args.dry_run):
                all_ok = False
            if not create_depot(team, args.dry_run):
                all_ok = False
            if not add_protection(team, team, args.dry_run):
                all_ok = False
            teams_processed.add(team)

        # 3. Utente nel gruppo
        if not add_user_to_group(username, team, args.dry_run):
            all_ok = False

        # 4. Nuovo status
        new_status = "created" if all_ok else "error"
        user["status"] = new_status
        status_updates.append({"username": username, "team": team, "status": new_status})

        if all_ok:
            success_count += 1
        else:
            error_count += 1

    # ── Aggiornamento status sul Worker ──
    if not args.dry_run and status_updates:
        print(f"\n{'═' * 50}")
        print(f"Aggiornamento status sul Worker ({len(status_updates)} record)...")
        try:
            result = naba_store.patch_status(admin_token, status_updates)
            print(f"  {result['updated']} record aggiornati")
            for f in result["failed"]:
                print(f"  ! {f.get('username', '?')}: {f.get('error', 'errore sconosciuto')}")
        except StoreError as e:
            print(f"  [ERRORE] Status non aggiornati: {e}")
            print("  Gli oggetti Perforce sono stati creati comunque.")
            print("  Controlla lo stato con: python scripts/kv_status.py")

    print(f"\n{'═' * 50}")
    print(f"PERFORCE: {success_count} riusciti, {error_count} errori")

    if args.dry_run:
        print("\n*** Era un dry run. Rilancia senza --dry-run per applicare. ***")

    # ── Discord + Email ──
    if success_count == 0 and not args.dry_run:
        print("\nNessun utente creato — Discord/Email saltati.")
        if args.export_xlsx:
            export_xlsx(rows, args.export_xlsx)
        return

    try:
        from discord_email_provision import provision_discord_and_email
    except ImportError as e:
        print(f"\n[ATTENZIONE] Import di discord_email_provision fallito: {e}")
        print("Verifica che discord_email_provision.py sia nella stessa cartella.")
        print("Discord/Email saltati.")
        if args.export_xlsx:
            export_xlsx(rows, args.export_xlsx)
        return

    # Solo gli utenti appena processati con successo, non tutto il KV.
    # Lo status in memoria è aggiornato anche in dry-run.
    target_users = [u for u in pending if u.get("status") == "created"]

    if not target_users:
        print("\nNessun utente da configurare su Discord/Email.")
        if args.export_xlsx:
            export_xlsx(rows, args.export_xlsx)
        return

    teams = {}
    for u in target_users:
        teams.setdefault(u["team"].strip(), []).append(u)

    print(f"\n{'═' * 50}")
    print(f"DISCORD + EMAIL ({len(target_users)} utenti, {len(teams)} team)")
    print(f"{'═' * 50}")

    discord_token = None
    resend_api_key = None

    if not args.skip_discord:
        discord_token = getpass.getpass("\nDiscord bot token (Invio per saltare): ")
        if not discord_token.strip():
            discord_token = None
            print("Discord saltato.")

    if not args.skip_email:
        resend_api_key = getpass.getpass("Resend API key (Invio per saltare): ")
        if not resend_api_key.strip():
            resend_api_key = None
            print("Email saltate.")

    if discord_token or resend_api_key:
        for team, team_users in teams.items():
            provision_discord_and_email(
                users=team_users,
                discord_token=discord_token,
                resend_api_key=resend_api_key,
                category_name=args.category,
                dry_run=args.dry_run,
            )
    else:
        print("\nEntrambi saltati — niente da fare.")

    if args.export_xlsx:
        export_xlsx(rows, args.export_xlsx)

    print(f"\n{'═' * 50}")
    print("FATTO!")


if __name__ == "__main__":
    main()
