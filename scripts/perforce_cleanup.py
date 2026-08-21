#!/usr/bin/env python3
"""
perforce_cleanup.py

Rimuove uno studente che si è ritirato, da Perforce e dal KV in un colpo solo.
È l'operazione inversa di perforce_provision.py.

Per ogni team dell'utente:
  1. Lo toglie dal gruppo Perforce (= il team)
  2. Revert + cancellazione dei suoi workspace e delle changelist pending
  3. Cancella l'account Perforce, se non gli resta accesso da nessun'altra parte
  4. Segna il record sul KV come 'removed' (o lo cancella, con --delete-record)

Cosa NON tocca, di proposito:
  - il depot del team, che contiene il lavoro degli altri
  - le protezioni, che sono sul gruppo e non sull'utente
  - Discord: ruoli, canali e inviti vanno rimossi a mano

Password Perforce e token admin vengono chiesti a runtime (input nascosto,
non salvati da nessuna parte).

Server, utente e password Perforce vengono chiesti in sequenza a ogni
esecuzione: l'indirizzo cambia con la rete da cui si lavora e dalla VLAN del
virtual studio il server si raggiunge solo per IP.

Uso:
    python perforce_cleanup.py --user mario_rossi --dry-run   # anteprima
    python perforce_cleanup.py --user mario_rossi             # rimozione
    python perforce_cleanup.py --user mario_rossi --team Alfa # solo da un team
    python perforce_cleanup.py --user mario_rossi --delete-record
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
# Server, utente e password vengono chiesti a ogni esecuzione: nel codice non
# resta né l'indirizzo del server né un account, e la repo è pubblica. Serve
# anche perché dalla VLAN del virtual studio il server si raggiunge solo per IP.
P4PORT = ""
P4USER = ""
P4PASSWD = ""

# Account che non vanno mai rimossi, qualunque cosa si scriva in --user.
# A runtime si aggiunge anche l'utente con cui ci si connette.
ALWAYS_KEEP = {
    "villal",
}
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


def ask_p4_connection() -> None:
    """
    Chiede server, utente e password, in quest'ordine, a ogni esecuzione.
    L'indirizzo cambia a seconda della rete da cui si lavora, quindi non ha
    un default: dalla VLAN del virtual studio va indicato per IP.
    """
    global P4PORT, P4USER, P4PASSWD

    P4PORT = input("Server Perforce (host:porta): ").strip()
    if not P4PORT:
        print("ERRORE: serve l'indirizzo del server.")
        sys.exit(1)

    P4USER = input("Utente Perforce: ").strip()
    if not P4USER:
        print("ERRORE: serve l'utente.")
        sys.exit(1)

    P4PASSWD = getpass.getpass(f"Password per {P4USER}: ")


def p4_user_exists(username: str) -> bool:
    return username in p4(f"users {username}").stdout


def p4_user_groups(username: str) -> list[str]:
    """Gruppi di cui l'utente è membro diretto."""
    result = p4(f"groups {username}")
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]


def remove_user_from_group(username: str, group_name: str, dry_run: bool = False) -> bool:
    """
    Toglie l'utente dalla sezione Users: dello spec del gruppo.

    Attenzione: se era l'ultimo membro, Perforce cancella il gruppo. Il depot e
    la riga nelle protezioni restano, e vanno bene così — il gruppo si ricrea da
    solo al prossimo provisioning sullo stesso team.
    """
    result = p4(f"group -o {group_name}")
    if result.returncode != 0:
        print(f"    [ERRORE] Gruppo '{group_name}' non leggibile: {result.stderr.strip()}")
        return False

    spec_lines = result.stdout.strip().split("\n")

    new_spec_lines = []
    in_users_section = False
    found = False
    remaining_users = 0

    for line in spec_lines:
        if line.startswith("Users:"):
            in_users_section = True
            new_spec_lines.append(line)
            continue

        if in_users_section:
            if line.startswith("\t"):
                if line.strip() == username:
                    found = True
                    continue  # la riga dell'utente non viene ricopiata
                remaining_users += 1
            else:
                in_users_section = False

        new_spec_lines.append(line)

    if not found:
        print(f"    [skip] '{username}' non è nel gruppo '{group_name}'")
        return True

    if dry_run:
        extra = " (ultimo membro: il gruppo verrebbe cancellato)" if remaining_users == 0 else ""
        print(f"    [dry-run] Toglierebbe '{username}' dal gruppo '{group_name}'{extra}")
        return True

    new_spec = "\n".join(new_spec_lines) + "\n"
    result = p4("group -i", stdin_text=new_spec)
    if result.returncode != 0:
        print(f"    [ERRORE] '{username}' non rimosso da '{group_name}': {result.stderr.strip()}")
        return False

    print(f"    [rimosso] '{username}' ← gruppo '{group_name}'")
    if remaining_users == 0:
        print(f"    [nota] '{group_name}' era rimasto senza membri: Perforce l'ha cancellato")
    return True


def get_user_workspaces(username: str) -> list[str]:
    result = p4(f"clients -u {username}")
    workspaces = []
    for line in result.stdout.strip().split("\n"):
        if line.startswith("Client "):
            workspaces.append(line.split(" ")[1])
    return workspaces


def get_pending_changes(username: str) -> list[str]:
    """Changelist pending dell'utente. Bloccano la cancellazione dell'account."""
    result = p4(f"changes -u {username} -s pending")
    changes = []
    for line in result.stdout.strip().split("\n"):
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "Change":
            changes.append(parts[1])
    return changes


def delete_pending_change(change: str, dry_run: bool = False) -> bool:
    if dry_run:
        return True
    # I file aperti vanno rilasciati prima che la changelist si possa cancellare.
    p4(f"revert -C {change} //...")
    result = p4(f"change -d -f {change}")
    return result.returncode == 0


def delete_workspace(ws_name: str, dry_run: bool = False) -> bool:
    if dry_run:
        return True
    p4(f"-c {ws_name} revert //...")
    result = p4(f"client -d -f {ws_name}")
    return result.returncode == 0


def delete_p4_user(username: str, dry_run: bool = False) -> bool:
    if dry_run:
        return True
    result = p4(f"user -d -f {username}")
    return result.returncode == 0


# ── Main ────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Rimuove uno studente da Perforce e dal KV",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Senza --team l'utente viene tolto da tutti i suoi team e l'account Perforce
viene cancellato. Con --team viene tolto solo da quel gruppo: l'account resta
in piedi se gli restano altri team.

Sul KV il record passa a 'removed' e resta consultabile. Con --delete-record
viene invece cancellato del tutto.

Server, utente e password Perforce vengono chiesti all'avvio, in quest'ordine.

Esempi:
  python perforce_cleanup.py --user mario_rossi --dry-run
  python perforce_cleanup.py --user mario_rossi
  python perforce_cleanup.py --user mario_rossi --team ProjectAlpha
  python perforce_cleanup.py --user mario_rossi --delete-record
        """,
    )
    parser.add_argument("--user", required=True, help="Username Perforce (es. mario_rossi)")
    parser.add_argument("--team", default=None, help="Rimuove solo da questo team")
    parser.add_argument("--dry-run", action="store_true", help="Anteprima senza modifiche")
    parser.add_argument("--delete-record", action="store_true",
                        help="Cancella il record dal KV invece di segnarlo 'removed'")
    parser.add_argument("--keep-account", action="store_true",
                        help="Non cancellare l'account Perforce, solo i gruppi")
    args = parser.parse_args()

    username = args.user.strip()
    team_filter = args.team.strip() if args.team else None

    if username.lower() in ALWAYS_KEEP:
        print(f"ERRORE: '{username}' è un account protetto, non si rimuove da qui.")
        sys.exit(1)

    # ── Dati dal Worker ──
    print(f"Worker: {naba_store.worker_url()}")
    admin_token = naba_store.get_admin_token()

    try:
        rows = naba_store.fetch_users(admin_token)
    except StoreError as e:
        print(f"ERRORE: {e}")
        sys.exit(1)

    matches = [r for r in rows if r.get("username", "").strip().lower() == username.lower()]
    if team_filter:
        matches = [r for r in matches if r.get("team", "").strip().lower() == team_filter.lower()]

    kv_teams = sorted({r.get("team", "").strip() for r in matches if r.get("team", "").strip()})

    print(f"Scaricati {len(rows)} record dal KV")
    if matches:
        print(f"  '{username}' presente in {len(matches)} record: {', '.join(kv_teams)}")
    else:
        # Può succedere: account creato a mano su Perforce, o record già cancellato.
        print(f"  '{username}' non è nel KV — si procede solo sul lato Perforce")
        if team_filter:
            print(f"  (nessun record per il team '{team_filter}')")

    # ── Server, utente, password Perforce ──
    print()
    ask_p4_connection()

    # Non ci si può cancellare l'account da sotto i piedi.
    if username.lower() == P4USER.lower():
        print(f"\nERRORE: '{username}' è l'account con cui sei connesso.")
        sys.exit(1)

    print(f"Connessione a {P4PORT}...")
    result = p4("info")
    if result.returncode != 0:
        print("ERRORE: connessione al server Perforce fallita.")
        print(f"  Errore: {result.stderr.strip()}")
        sys.exit(1)
    print("Connesso al server Perforce")

    if args.dry_run:
        print("\n*** DRY RUN — nessuna modifica verrà applicata ***")

    # ── Cosa c'è da rimuovere ──
    exists_on_p4 = p4_user_exists(username)
    p4_groups = p4_user_groups(username) if exists_on_p4 else []

    if team_filter:
        # Solo il gruppo indicato, e solo se l'utente ci sta davvero dentro.
        target_groups = [g for g in p4_groups if g.lower() == team_filter.lower()]
    else:
        target_groups = list(p4_groups)

    leftover_groups = [g for g in p4_groups if g not in target_groups]
    workspaces = get_user_workspaces(username) if exists_on_p4 else []
    pending_changes = get_pending_changes(username) if exists_on_p4 else []

    # L'account si cancella solo se non gli resta accesso da nessuna parte.
    drop_account = exists_on_p4 and not args.keep_account and not leftover_groups

    print(f"\n{'═' * 60}")
    print(f"RIMOZIONE: {username}")
    print(f"{'═' * 60}")

    if not exists_on_p4:
        print("  Account Perforce: non esiste (mai creato, o già rimosso)")
    else:
        print(f"  Gruppi da cui esce:   {', '.join(target_groups) if target_groups else '—'}")
        print(f"  Gruppi che restano:   {', '.join(leftover_groups) if leftover_groups else '—'}")
        print(f"  Workspace:            {len(workspaces)}")
        print(f"  Changelist pending:   {len(pending_changes)}")
        print(f"  Account Perforce:     {'CANCELLATO' if drop_account else 'mantenuto'}")

    if matches:
        kv_action = "record CANCELLATI" if args.delete_record else "status → removed"
        print(f"  Record sul KV:        {len(matches)} ({kv_action})")
    else:
        print("  Record sul KV:        nessuno")

    if not exists_on_p4 and not matches:
        print("\nNiente da rimuovere.")
        return

    if leftover_groups and not args.keep_account:
        print(f"\n  Nota: l'account resta perché '{username}' è ancora in "
              f"{len(leftover_groups)} gruppo/i.")

    # ── Conferma ──
    if not args.dry_run:
        print(f"\n⚠️  Operazione irreversibile su Perforce e sul KV.")
        confirm = input("Scrivi CONFIRM per procedere: ").strip()
        if confirm != "CONFIRM":
            print("Annullato.")
            return

    errors = 0

    # ── Perforce ──
    if exists_on_p4:
        print(f"\n{'─' * 50}")
        print("Perforce")

        for group in target_groups:
            if not remove_user_from_group(username, group, args.dry_run):
                errors += 1

        if drop_account:
            for change in pending_changes:
                if delete_pending_change(change, args.dry_run):
                    print(f"    [{'dry-run' if args.dry_run else 'cancellata'}] Changelist {change}")
                else:
                    print(f"    [ERRORE] Changelist {change} non cancellata")
                    errors += 1

            for ws in workspaces:
                if delete_workspace(ws, args.dry_run):
                    print(f"    [{'dry-run' if args.dry_run else 'cancellato'}] Workspace '{ws}'")
                else:
                    print(f"    [ERRORE] Workspace '{ws}' non cancellato")
                    errors += 1

            if delete_p4_user(username, args.dry_run):
                print(f"    [{'dry-run' if args.dry_run else 'cancellato'}] Utente '{username}'")
            else:
                print(f"    [ERRORE] Utente '{username}' non cancellato")
                errors += 1

    # ── KV ──
    if matches and not args.dry_run:
        print(f"\n{'─' * 50}")
        print("KV")
        try:
            if args.delete_record:
                deleted = naba_store.delete_user(admin_token, username, team_filter)
                print(f"    [cancellati] {deleted} record")
            else:
                updates = [
                    {"username": username, "team": r["team"].strip(), "status": "removed"}
                    for r in matches
                ]
                result = naba_store.patch_status(admin_token, updates)
                print(f"    [aggiornati] {result['updated']} record → removed")
                for f in result["failed"]:
                    print(f"    ! {f.get('username', '?')}: {f.get('error', 'errore sconosciuto')}")
                    errors += 1
        except StoreError as e:
            print(f"    [ERRORE] KV non aggiornato: {e}")
            print("    Gli oggetti Perforce sono già stati rimossi.")
            print("    Rilancia solo la parte KV, o controlla con kv_status.py")
            errors += 1
    elif matches and args.dry_run:
        print(f"\n{'─' * 50}")
        print("KV")
        action = "cancellerebbe" if args.delete_record else "porterebbe a 'removed'"
        print(f"    [dry-run] {action} {len(matches)} record")

    # ── Esito ──
    print(f"\n{'═' * 60}")
    if errors:
        print(f"COMPLETATO CON {errors} ERRORE/I — rileggi l'output sopra")
    else:
        print("COMPLETATO")

    if args.dry_run:
        print("\n*** Era un dry run. Rilancia senza --dry-run per applicare. ***")
    elif not args.delete_record and matches:
        # Il KV è eventually consistent: l'export può restare indietro.
        print("\nIl KV può metterci qualche decina di secondi ad allinearsi.")
        print("Verifica con: python scripts/kv_status.py")


if __name__ == "__main__":
    main()
