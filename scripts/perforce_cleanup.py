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
  - Discord: ruoli, canali e inviti vanno rimossi a mano, e a fine esecuzione
    lo script stampa cosa cercare

Token admin, server, utente e password Perforce vengono chiesti in sequenza a
ogni esecuzione e non sono salvati da nessuna parte. L'indirizzo del server
cambia con la rete da cui si lavora: dalla VLAN del virtual studio va indicato
per IP.

Uso:
    python perforce_cleanup.py --user mario_rossi --dry-run   # anteprima
    python perforce_cleanup.py --user mario_rossi             # rimozione
    python perforce_cleanup.py --user mario_rossi --team Alfa # solo da un team
    python perforce_cleanup.py --user mario_rossi --delete-record
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import naba_store
import p4_common as p4c
from naba_store import StoreError
from p4_common import P4Error


# ══════════════════════════════════════════════════════════════
# CONFIGURAZIONE
# ══════════════════════════════════════════════════════════════
# Nessun account protetto è scritto qui: l'unico che va difeso sempre è quello
# con cui ci si connette, e quello si conosce solo a runtime. Gli account di
# servizio che non devono mai sparire si passano a perforce_prune.py con --keep.
#
# La connessione viene creata in main() e passata alle funzioni di p4_common.
P4 = None
# ══════════════════════════════════════════════════════════════


def find_kv_matches(rows: list[dict], username: str, team: str = None) -> list[dict]:
    """
    I record del KV che riguardano questo utente, eventualmente ristretti a un
    team. Il confronto è case-insensitive e ignora gli spazi ai bordi: i valori
    arrivano da un form pubblico e non sono normalizzati.
    """
    matches = [
        r for r in rows
        if r.get("username", "").strip().lower() == username.strip().lower()
    ]
    if team:
        matches = [
            r for r in matches
            if r.get("team", "").strip().lower() == team.strip().lower()
        ]
    return matches


# ── Main ────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Rimuove uno studente da Perforce e dal KV",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Senza --team l'utente viene tolto da tutti i suoi team e l'account Perforce
viene cancellato. Con --team viene tolto solo da quel gruppo: l'account resta
in piedi se gli restano altri team, ma i workspace e le changelist mappati sul
depot di quel team vengono comunque rimossi.

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

    # I due valori finiscono in comandi p4: si validano prima di ogni altra cosa.
    try:
        username = p4c.check_p4_name(args.user, "username")
        team_filter = p4c.check_p4_name(args.team, "team") if args.team else None
    except P4Error as e:
        print(f"ERRORE: {e}")
        sys.exit(1)

    # ── Dati dal Worker ──
    print(f"Worker: {naba_store.worker_url()}")
    admin_token = naba_store.get_admin_token()

    try:
        rows = naba_store.fetch_users(admin_token)
    except StoreError as e:
        print(f"ERRORE: {e}")
        sys.exit(1)

    matches = find_kv_matches(rows, username, team_filter)
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
    global P4
    print()
    P4 = p4c.ask_p4_connection()

    # Non ci si può cancellare l'account da sotto i piedi.
    if username.lower() == P4.user.lower():
        print(f"\nERRORE: '{username}' è l'account con cui sei connesso.")
        sys.exit(1)

    print(f"Connessione a {P4.port}...")
    result = p4c.connect(P4)
    if result.returncode != 0:
        print("ERRORE: connessione al server Perforce fallita.")
        print(f"  Errore: {result.stderr.strip()}")
        sys.exit(1)
    print("Connesso al server Perforce")

    if args.dry_run:
        print("\n*** DRY RUN — nessuna modifica verrà applicata ***")

    # ── Cosa c'è da rimuovere ──
    exists_on_p4 = p4c.user_exists(P4, username)
    p4_groups = p4c.user_groups(P4, username) if exists_on_p4 else []

    if team_filter:
        # Solo il gruppo indicato, e solo se l'utente ci sta davvero dentro.
        target_groups = [g for g in p4_groups if g.lower() == team_filter.lower()]
    else:
        target_groups = list(p4_groups)

    leftover_groups = [g for g in p4_groups if g not in target_groups]

    # L'account si cancella solo se non gli resta accesso da nessuna parte.
    drop_account = exists_on_p4 and not args.keep_account and not leftover_groups

    all_workspaces = p4c.user_workspaces(P4, username) if exists_on_p4 else []
    all_changes = p4c.pending_changes(P4, username) if exists_on_p4 else []

    if drop_account:
        workspaces = all_workspaces
        changes = all_changes
    elif team_filter and exists_on_p4:
        # Rimozione parziale: l'account resta, ma i workspace mappati sul depot
        # di quel team vanno via lo stesso. Se non lo facciamo qui non li pulisce
        # più nessuno: perforce_prune.py vede l'utente come "con accesso" grazie
        # agli altri team e non lo tocca mai.
        workspaces = [
            ws for ws in all_workspaces
            if team_filter in p4c.workspace_depots(P4, ws)
        ]
        targeted = set(workspaces)
        changes = [c for c in all_changes if p4c.change_client(P4, c) in targeted]
    else:
        workspaces = []
        changes = []

    print(f"\n{'═' * 60}")
    print(f"RIMOZIONE: {username}")
    print(f"{'═' * 60}")

    if not exists_on_p4:
        print("  Account Perforce: non esiste (mai creato, o già rimosso)")
    else:
        print(f"  Gruppi da cui esce:   {', '.join(target_groups) if target_groups else '—'}")
        print(f"  Gruppi che restano:   {', '.join(leftover_groups) if leftover_groups else '—'}")
        print(f"  Workspace da pulire:  {len(workspaces)} di {len(all_workspaces)}")
        print(f"  Changelist pending:   {len(changes)} di {len(all_changes)}")
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
        print("\n⚠️  Operazione irreversibile su Perforce e sul KV.")
        confirm = input("Scrivi CONFIRM per procedere: ").strip()
        if confirm != "CONFIRM":
            print("Annullato.")
            return

    errors = 0
    tag = "dry-run" if args.dry_run else None

    # ── Perforce ──
    if exists_on_p4:
        print(f"\n{'─' * 50}")
        print("Perforce")

        for group in target_groups:
            ok, err, was_member, remaining = p4c.remove_user_from_group(
                P4, username, group, args.dry_run
            )
            if not ok:
                print(f"    [ERRORE] '{username}' non rimosso da '{group}': {err}")
                errors += 1
            elif not was_member:
                print(f"    [skip] '{username}' non è nel gruppo '{group}'")
            elif args.dry_run:
                extra = " (ultimo membro: il gruppo verrebbe cancellato)" if remaining == 0 else ""
                print(f"    [dry-run] Toglierebbe '{username}' dal gruppo '{group}'{extra}")
            else:
                print(f"    [rimosso] '{username}' ← gruppo '{group}'")
                if remaining == 0:
                    print(f"    [nota] '{group}' era rimasto senza membri: Perforce l'ha cancellato")

        for change in changes:
            ok, err = p4c.delete_pending_change(P4, change, args.dry_run)
            if ok:
                print(f"    [{tag or 'cancellata'}] Changelist {change}")
            else:
                print(f"    [ERRORE] Changelist {change} non cancellata: {err}")
                errors += 1

        for ws in workspaces:
            ok, err = p4c.delete_workspace(P4, ws, args.dry_run)
            if ok:
                print(f"    [{tag or 'cancellato'}] Workspace '{ws}'")
            else:
                print(f"    [ERRORE] Workspace '{ws}' non cancellato: {err}")
                errors += 1

        if drop_account:
            ok, err = p4c.delete_user(P4, username, args.dry_run)
            if ok:
                print(f"    [{tag or 'cancellato'}] Utente '{username}'")
            else:
                print(f"    [ERRORE] Utente '{username}' non cancellato: {err}")
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

    # ── Cosa resta da fare a mano ──
    residual_teams = kv_teams or target_groups
    print(f"\n{'─' * 50}")
    print("Da rimuovere a mano, lo script non tocca Discord:")
    if residual_teams:
        for team in residual_teams:
            print(f"    ruolo e canale del team '{team}' — e '{username}' dal server")
    else:
        print(f"    ruolo, canale e presenza di '{username}' sul server Discord")

    if args.dry_run:
        print("\n*** Era un dry run. Rilancia senza --dry-run per applicare. ***")
    elif not args.delete_record and matches:
        # Il KV è eventually consistent: l'export può restare indietro.
        print("\nIl KV può metterci qualche decina di secondi ad allinearsi.")
        print("Verifica con: python scripts/kv_status.py")


if __name__ == "__main__":
    main()
