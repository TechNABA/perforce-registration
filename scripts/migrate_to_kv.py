#!/usr/bin/env python3
"""
migrate_to_kv.py

Migrazione una tantum: carica il vecchio users.csv nel KV Cloudflare
tramite l'endpoint /import del Worker, preservando gli status esistenti.

Il CSV non sta più nella repo: viene cercato nel backup locale
(~/Documents/NABA-perforce-backup/users.csv).

Da eseguire una volta sola, dopo aver deployato il nuovo worker.js e
configurato binding USERS + secret ADMIN_TOKEN.

Uso:
    python scripts/migrate_to_kv.py --dry-run       # controlla il CSV, non scrive
    python scripts/migrate_to_kv.py                 # migra
    python scripts/migrate_to_kv.py --csv path.csv  # sorgente alternativa

A migrazione fatta questo script si può eliminare.
"""

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import naba_store
from naba_store import FIELDS, VALID_STATUS, StoreError


def find_csv(custom: Path = None) -> Path:
    if custom:
        if custom.exists():
            return custom
        print(f"ERRORE: CSV non trovato: {custom}")
        sys.exit(1)

    candidates = [
        Path.home() / "Documents" / "NABA-perforce-backup" / "users.csv",
        Path("users.csv"),
        Path.home() / "Downloads" / "users.csv",
    ]
    for p in candidates:
        if p.exists():
            return p

    print("ERRORE: users.csv non trovato. Percorsi provati:")
    for p in candidates:
        print(f"  {p}")
    print("\nIndicalo con --csv /percorso/users.csv")
    sys.exit(1)


def read_rows(path: Path) -> list[dict]:
    with open(path, "r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def validate(rows: list[dict]) -> tuple[list[dict], list[str]]:
    """Normalizza le righe e segnala quelle che il Worker rifiuterebbe."""
    good, problems = [], []
    seen = set()

    for i, row in enumerate(rows, start=2):  # riga 1 = intestazione
        record = {f: (row.get(f) or "").strip() for f in FIELDS}
        username = record["username"]

        if not username:
            problems.append(f"riga {i}: username mancante")
            continue
        if not record["full_name"]:
            problems.append(f"riga {i}: full_name mancante ({username})")
            continue
        if "@" not in record["email"]:
            problems.append(f"riga {i}: email non valida ({username})")
            continue
        if not record["team"]:
            problems.append(f"riga {i}: team mancante ({username})")
            continue

        status = record["status"].lower()
        if status not in VALID_STATUS:
            problems.append(f"riga {i}: status '{status}' non valido → forzato a 'pending' ({username})")
            record["status"] = "pending"
        else:
            record["status"] = status

        key = (username.lower(), record["team"].lower())
        if key in seen:
            problems.append(f"riga {i}: coppia utente/team duplicata nel CSV ({username}) → saltata")
            continue
        seen.add(key)

        good.append(record)

    return good, problems


def main():
    parser = argparse.ArgumentParser(description="Migra users.csv nel KV Cloudflare")
    parser.add_argument("--csv", type=Path, default=None, help="Percorso del CSV sorgente")
    parser.add_argument("--dry-run", action="store_true", help="Controlla senza scrivere")
    parser.add_argument("--skip-existing", action="store_true",
                        help="Non sovrascrive record già presenti nel KV")
    args = parser.parse_args()

    csv_path = find_csv(args.csv)
    rows = read_rows(csv_path)
    print(f"Sorgente: {csv_path} ({len(rows)} righe)")

    records, problems = validate(rows)

    if problems:
        print(f"\n{len(problems)} segnalazioni:")
        for p in problems:
            print(f"  ! {p}")

    if not records:
        print("\nNessun record valido da migrare.")
        sys.exit(1)

    # Riepilogo senza nomi né email.
    by_status, by_team = {}, {}
    for r in records:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        by_team[r["team"]] = by_team.get(r["team"], 0) + 1

    print(f"\n{len(records)} record pronti")
    print("  per status: " + ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())))
    print("  per team:   " + ", ".join(f"{k}={v}" for k, v in sorted(by_team.items())))

    if args.dry_run:
        print("\n*** Dry run — niente è stato scritto sul KV ***")
        return

    print(f"\nWorker: {naba_store.worker_url()}")
    token = naba_store.get_admin_token()

    # Verifica token e stato di partenza prima di scrivere.
    try:
        before = len(naba_store.fetch_users(token))
    except StoreError as e:
        print(f"ERRORE: {e}")
        sys.exit(1)

    print(f"Connesso. Record già nel KV: {before}")

    if before > 0 and not args.skip_existing:
        risposta = input(
            f"Il KV contiene già {before} record e verranno sovrascritti "
            f"in caso di stesse chiavi. Continuare? [s/N] "
        ).strip().lower()
        if risposta not in ("s", "si", "sì", "y", "yes"):
            print("Annullato.")
            return

    print(f"\nInvio di {len(records)} record...")
    try:
        result = naba_store.import_users(
            token, records, skip_existing=args.skip_existing
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

    # Il KV è eventually consistent: la list() può restare indietro.
    print("\nVerifica...")
    expected = before + result["written"] if args.skip_existing else len(records)
    total = naba_store.wait_for_count(token, expected)

    print(f"\n{'═' * 50}")
    if total >= expected:
        print(f"Migrazione completata: {total} record nel KV.")
        print("\nProssimi passi: bonificare la history git e revocare il PAT GitHub")
        print("(vedi CLOUDFLARE.md, sezione finale).")
        print(f"Il backup locale resta in {Path.home() / 'Documents' / 'NABA-perforce-backup'}")
    else:
        print(f"ATTENZIONE: nel KV risultano {total} record, ne erano attesi {expected}.")
        print("Il KV è eventually consistent: riprova tra un minuto con")
        print("  python scripts/kv_status.py")
        print("Se il numero non sale, controlla i record rifiutati qui sopra.")


if __name__ == "__main__":
    main()
