#!/usr/bin/env python3
"""
perforce_provision.py

Scarica gli utenti dal Worker Cloudflare e, per ognuno con status 'pending':
  1. Prepara il team, una volta sola: protezione write del gruppo sul depot,
     poi gruppo e depot locale (col nome del team) se non esistono. Un gruppo
     o un depot già esistente senza quella protezione, come 'depot', non è di
     questo flusso: il team viene rifiutato e i suoi record vanno in 'error'
  2. Crea l'utente Perforce
  3. Aggiunge l'utente al gruppo
  4. Aggiorna lo status a 'created' sul Worker

Token admin, server, utente e password Perforce vengono chiesti in sequenza a
ogni esecuzione, e non sono salvati da nessuna parte. L'indirizzo del server
cambia con la rete da cui si lavora: dalla VLAN del virtual studio va indicato
per IP.

Uso:
    python perforce_provision.py                            # provisioning
    python perforce_provision.py --dry-run                  # anteprima
    python perforce_provision.py --skip-discord             # niente ruoli/canali
    python perforce_provision.py --skip-email               # niente email
    python perforce_provision.py --category Tesi            # categoria Discord
    python perforce_provision.py --export-xlsx utenti.xlsx  # XLSX in locale
    python perforce_provision.py --retry-errors             # riprova anche gli 'error'

La password iniziale degli account studente si digita a runtime insieme alle
altre credenziali: da riga di comando resterebbe leggibile negli argomenti del
processo e nella cronologia della shell.
"""

import argparse
import getpass
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
# Server, utente e password vengono chiesti a ogni esecuzione: nel codice non
# resta né l'indirizzo del server né un account, e la repo è pubblica. Serve
# anche perché dalla VLAN del virtual studio il server si raggiunge solo per IP.
#
# La connessione viene creata in main() e usata dalle funzioni qui sotto.
P4 = None
# ══════════════════════════════════════════════════════════════


# ── helper p4 ───────────────────────────────────────────────────
def p4_user_exists(username: str) -> bool:
    return username in P4.run("users", username).stdout


def list_groups() -> list[str] | None:
    """I gruppi del server, None se l'elenco non si legge."""
    result = P4.run("groups")
    if result.returncode != 0:
        print(f"    [ERRORE] Elenco dei gruppi non leggibile: {result.stderr.strip()}")
        return None
    return result.stdout.split()


def list_depots() -> list[str] | None:
    """I depot del server, None se l'elenco non si legge."""
    result = P4.run("depots")
    if result.returncode != 0:
        print(f"    [ERRORE] Elenco dei depot non leggibile: {result.stderr.strip()}")
        return None
    return [parts[1] for parts in (line.split() for line in result.stdout.split("\n"))
            if len(parts) >= 2 and parts[0] == "Depot"]


def find_name(names: list[str], name: str) -> str | None:
    """Il nome come lo scrive il server, senza distinguere maiuscole."""
    return next((n for n in names if n.lower() == name.lower()), None)


# Le due verifiche non distinguono maiuscole: su un server case-insensitive un
# "group -i" per "projectalpha" sovrascriverebbe ProjectAlpha svuotandolo, e
# "DEPOT" è il depot di default. None se l'elenco non si legge: chi chiama non
# deve scambiarlo né per "esiste" né per "non esiste".
def p4_group_exists(group_name: str) -> bool | None:
    groups = list_groups()
    return None if groups is None else find_name(groups, group_name) is not None


def p4_depot_exists(depot_name: str) -> bool | None:
    depots = list_depots()
    return None if depots is None else find_name(depots, depot_name) is not None


def canonical_team(team: str, known=()) -> str:
    """
    Il nome del team come lo conosce già il server, o come è già stato scelto
    in questo giro (`known`). Gli studenti scrivono lo stesso team in modi
    diversi: così finiscono tutti nello stesso gruppo. La grafia esatta vince,
    perché su un server case-sensitive "staff" e "Staff" sono due gruppi diversi.
    """
    names = [*(list_groups() or []), *(list_depots() or []), *known]
    if team in names:
        return team
    return find_name(names, team) or team


def create_user(username: str, full_name: str, email: str, password: str = None, dry_run: bool = False) -> bool:
    if p4_user_exists(username):
        print(f"    [skip] Utente '{username}' già esistente")
        return True

    # Nome ed email arrivano dal form pubblico: tab e a capo spezzerebbero lo
    # spec e permetterebbero di iniettare campi che non abbiamo scritto noi.
    spec = (
        f"User:\t{username}\n"
        f"Email:\t{p4c.clean_spec_value(email)}\n"
        f"FullName:\t{p4c.clean_spec_value(full_name)}\n"
    )

    if dry_run:
        print(f"    [dry-run] Creerebbe l'utente '{username}'")
        return True

    result = P4.run("user", "-f", "-i", stdin_text=spec)
    if result.returncode != 0:
        print(f"    [ERRORE] Creazione utente '{username}' fallita: {result.stderr.strip()}")
        return False

    print(f"    [creato] Utente '{username}'")

    if password:
        result = P4.run("-u", username, "passwd", stdin_text=f"{password}\n{password}\n")
        if result.returncode == 0:
            print(f"    [password] Impostata per '{username}'")
        else:
            print(f"    [ATTENZIONE] Password non impostata per '{username}': {result.stderr.strip()}")

    return True


def create_group(group_name: str, dry_run: bool = False) -> bool:
    exists = p4_group_exists(group_name)
    if exists is None:
        return False
    if exists:
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

    result = P4.run("group", "-i", stdin_text=spec)
    if result.returncode != 0:
        print(f"    [ERRORE] Creazione gruppo '{group_name}' fallita: {result.stderr.strip()}")
        return False

    print(f"    [creato] Gruppo '{group_name}'")
    return True


def add_user_to_group(username: str, group_name: str, dry_run: bool = False) -> bool:
    result = P4.run("group", "-o", group_name)
    if result.returncode != 0:
        print(f"    [ERRORE] Gruppo '{group_name}' non leggibile: {result.stderr.strip()}")
        return False

    new_spec, already_there = p4c.add_user_to_group_spec(result.stdout, username)

    if already_there:
        print(f"    [skip] Utente '{username}' già nel gruppo '{group_name}'")
        return True

    if dry_run:
        print(f"    [dry-run] Aggiungerebbe '{username}' al gruppo '{group_name}'")
        return True

    result = P4.run("group", "-i", stdin_text=new_spec)
    if result.returncode != 0:
        print(f"    [ERRORE] '{username}' non aggiunto al gruppo '{group_name}': {result.stderr.strip()}")
        return False

    print(f"    [aggiunto] '{username}' → gruppo '{group_name}'")
    return True


def create_depot(depot_name: str, dry_run: bool = False) -> bool:
    exists = p4_depot_exists(depot_name)
    if exists is None:
        return False
    if exists:
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

    result = P4.run("depot", "-i", stdin_text=spec)
    if result.returncode != 0:
        print(f"    [ERRORE] Creazione depot '{depot_name}' fallita: {result.stderr.strip()}")
        return False

    print(f"    [creato] Depot '//{depot_name}/...'")
    return True


def add_protection(group_name: str, depot_name: str, protect_spec: str,
                   dry_run: bool = False) -> bool:
    """Aggiunge la riga allo spec letto da setup_team, che ha già verificato che manchi."""
    prot_line = f"\twrite group {group_name} * //{depot_name}/..."

    if dry_run:
        print(f"    [dry-run] Aggiungerebbe write: gruppo '{group_name}' → '//{depot_name}/...'")
        return True

    new_spec = protect_spec.rstrip() + "\n" + prot_line + "\n"

    result = P4.run("protect", "-i", stdin_text=new_spec)
    if result.returncode != 0:
        print(f"    [ERRORE] Aggiornamento protezioni fallito: {result.stderr.strip()}")
        return False

    print(f"    [protect] write group:{group_name} → //{depot_name}/...")
    return True


def has_team_protection(protect_spec: str, team: str) -> bool:
    """
    La riga 'write group <team> * //<team>/...', esattamente così: su un
    server case-sensitive la riga di "staff" non dice niente del gruppo "Staff".
    """
    wanted = f"write group {team} * //{team}/..."
    return any(line.strip() == wanted for line in protect_spec.split("\n"))


def setup_team(team: str, dry_run: bool = False) -> bool:
    """
    Protezione, gruppo e depot del team, in quest'ordine.

    La riga di protezione è il segno che il team è di questo flusso, e si
    scrive per prima: un giro interrotto a metà si riprende rilanciando. Un
    gruppo o un depot che esiste già senza quella riga non è nostro (es.
    'depot', il depot di default, o un gruppo di amministratori): il team
    viene rifiutato, perché altrimenti chi lo sceglie otterrebbe la scrittura
    su un depot altrui o i permessi di un gruppo altrui.
    """
    result = P4.run("protect", "-o")
    if result.returncode != 0:
        print(f"    [ERRORE] Protezioni non leggibili: {result.stderr.strip()}")
        return False

    if not has_team_protection(result.stdout, team):
        group_exists, depot_exists = p4_group_exists(team), p4_depot_exists(team)
        if group_exists is None or depot_exists is None:
            return False  # elenco non leggibile: l'errore l'ha già stampato list_*
        taken = [what for what, exists in (("gruppo", group_exists),
                                           ("depot", depot_exists)) if exists]
        if taken:
            print(f"    [ERRORE] Team '{team}': {' e '.join(taken)} già esistente e non creato "
                  f"da questo flusso (manca 'write group {team} * //{team}/...'). Team non "
                  f"configurato: se deve lavorarci davvero, aggiungi la riga a mano e rilancia "
                  f"con --retry-errors")
            return False
        if not add_protection(team, team, result.stdout, dry_run):
            return False

    group_ok = create_group(team, dry_run)
    depot_ok = create_depot(team, dry_run)
    return group_ok and depot_ok


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


def ask_initial_password() -> str | None:
    """Chiede la password iniziale con conferma: non viene mai mostrata né
    inviata allo studente dallo script, quindi un errore di battitura
    resterebbe silenzioso senza una seconda lettura."""
    password = getpass.getpass(
        "Password iniziale per i nuovi utenti (Invio per non impostarla): "
    )
    if not password:
        return None

    confirm = getpass.getpass("Ripeti la password: ")
    if confirm != password:
        print("ERRORE: le due password non coincidono.")
        sys.exit(1)

    return password


# ── Main ────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Provisioning utenti Perforce dai dati sul Worker Cloudflare",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Server, utente e password Perforce vengono chiesti all'avvio, in quest'ordine.
Anche la password iniziale degli studenti si digita a runtime: da riga di
comando resterebbe leggibile negli argomenti del processo e nella cronologia
della shell.

Esempi:
  python perforce_provision.py --dry-run
  python perforce_provision.py
  python perforce_provision.py --skip-discord --skip-email
  python perforce_provision.py --category Tesi
  python perforce_provision.py --export-xlsx ~/Desktop/utenti.xlsx
  python perforce_provision.py --retry-errors
        """,
    )
    parser.add_argument("--dry-run", action="store_true", help="Anteprima senza modifiche")
    parser.add_argument("--skip-discord", action="store_true", help="Salta creazione ruoli/canali Discord")
    parser.add_argument("--skip-email", action="store_true", help="Salta invio email di benvenuto")
    parser.add_argument("--category", type=str, default="Tesi",
                        help="Categoria Discord per i nuovi canali (default: Tesi)")
    parser.add_argument("--retry-errors", action="store_true",
                        help="Riprova anche i record in 'error' (es. dopo aver sistemato un team rifiutato)")
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

    # ── Server, utente, password Perforce ──
    global P4
    print()
    P4 = p4c.ask_p4_connection()

    # Password iniziale degli account studente: chiesta qui, mai da riga di
    # comando, dove sarebbe leggibile da chiunque altro usi la macchina.
    initial_password = ask_initial_password()

    print(f"Connessione a {P4.port}...")
    result = p4c.connect(P4)
    if result.returncode != 0:
        print("ERRORE: connessione al server Perforce fallita.")
        print(f"  Server: {P4.port}")
        print(f"  Utente: {P4.user}")
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

    # Con --retry-errors ripassano anche i record in 'error': è il modo di
    # riprendere un team rifiutato o fallito dopo averlo sistemato.
    to_process = {"pending", "error"} if args.retry_errors else {"pending"}
    pending = [r for r in rows if r.get("status", "").strip().lower() in to_process]

    if not pending:
        print("\nNessun utente pending da processare.")
        if args.export_xlsx:
            export_xlsx(rows, args.export_xlsx)
        return

    print(f"\n{len(pending)} utente/i da processare:\n")

    # Le varianti di maiuscole scritte dagli studenti finiscono sullo stesso
    # nome anche in dry-run e quando il gruppo non esiste ancora: canonical_team
    # conosce anche i nomi già scelti in questo giro. Rifiuti e cache vanno per
    # nome risolto, non per minuscole: su un server case-sensitive "Staff" e
    # "staff" sono due team diversi e l'uno non deve decidere per l'altro.
    resolved = {}         # team come scritto nel record → nome usato su Perforce
    failed_teams = set()  # nomi (già risolti) dei team rifiutati o non configurati
    status_updates = []
    success_count = 0
    error_count = 0

    for user in pending:
        full_name = user["full_name"].strip()
        email = user["email"].strip()

        # Username e team finiscono in comandi p4 e negli spec: un valore che
        # non passa la validazione non arriva mai al server, il record va in
        # 'error' e il giro prosegue con gli altri.
        try:
            username = p4c.check_p4_name(user["username"], "username")
            team = p4c.check_p4_name(user["team"], "team")
        except P4Error as e:
            bad_username = user.get("username", "").strip()
            print(f"{'─' * 50}")
            print(f"Elaborazione: {bad_username!r}")
            print(f"    [ERRORE] Record scartato: {e}")
            user["status"] = "error"
            status_updates.append({
                "username": bad_username,
                "team": user.get("team", "").strip(),
                "status": "error",
            })
            error_count += 1
            continue

        print(f"{'─' * 50}")
        # Nome completo ed email non vanno a schermo: restano solo nel record.
        print(f"Elaborazione: {username}")
        print(f"  Team: {team} | Tesista: {user.get('tesista', 'no')} | Anno: {user.get('anno_corso', '') or '—'}")

        all_ok = True

        # 1. Protezione, gruppo e depot, una volta per team e prima degli
        # account: per un team rifiutato non si crea nessun utente, che
        # occuperebbe una licenza senza avere accesso a niente.
        if team not in resolved:
            name = canonical_team(team, resolved.values())
            if name not in resolved.values() and not setup_team(name, args.dry_run):
                failed_teams.add(name)
            resolved[team] = name
        team = resolved[team]
        user["team"] = team  # Discord ed email usano lo stesso nome di Perforce

        if team in failed_teams:
            print(f"    [ERRORE] Team '{team}' non configurato: '{username}' non creato")
            all_ok = False
        else:
            # 2. Utente
            if not create_user(username, full_name, email, initial_password, args.dry_run):
                all_ok = False

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
