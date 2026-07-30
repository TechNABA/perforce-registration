#!/usr/bin/env python3
"""
kv_status.py

Ispeziona il contenuto del KV senza stampare nomi né email, ed esporta
in locale quando serve. È anche il modo più rapido per verificare che
un Worker appena deployato sia configurato bene: se il binding o il
secret sono sbagliati, l'errore lo dice.

Uso:
    python scripts/kv_status.py                  # riepilogo aggregato
    python scripts/kv_status.py --usernames      # elenca anche gli username
    python scripts/kv_status.py --xlsx out.xlsx  # XLSX formattato
    python scripts/kv_status.py --csv out.csv    # dump grezzo
    python scripts/kv_status.py --status pending # filtra per status

I file esportati contengono dati personali: .gitignore li esclude,
ma vanno comunque tenuti fuori dalla repo.
"""

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import naba_store
from naba_store import FIELDS, StoreError


def main():
    parser = argparse.ArgumentParser(description="Stato del KV utenti")
    parser.add_argument("--usernames", action="store_true",
                        help="Elenca gli username (niente nomi completi né email)")
    parser.add_argument("--csv", type=Path, default=None,
                        help="Dump grezzo in CSV (contiene dati personali)")
    parser.add_argument("--xlsx", type=Path, default=None,
                        help="XLSX formattato e raggruppato per team (contiene dati personali)")
    parser.add_argument("--status", type=str, default=None,
                        help="Filtra per status (pending, created, existing, ...)")
    args = parser.parse_args()

    print(f"Worker: {naba_store.worker_url()}")
    token = naba_store.get_admin_token()

    try:
        users = naba_store.fetch_users(token, status=args.status)
    except StoreError as e:
        print(f"ERRORE: {e}")
        sys.exit(1)

    print(f"\nRecord nel KV: {len(users)}")
    if not users:
        return

    by_status, by_team = {}, {}
    for u in users:
        by_status[u["status"]] = by_status.get(u["status"], 0) + 1
        by_team[u["team"]] = by_team.get(u["team"], 0) + 1

    print("\nPer status:")
    for k, v in sorted(by_status.items()):
        print(f"  {k:<12} {v}")

    print("\nPer team:")
    for k, v in sorted(by_team.items()):
        print(f"  {k:<24} {v}")

    if args.usernames:
        print("\nUsername:")
        for u in sorted(users, key=lambda r: (r["team"].lower(), r["username"].lower())):
            print(f"  {u['username']:<28} {u['team']:<20} {u['status']}")

    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(users)
        print(f"\nCSV scritto: {args.csv} — contiene dati personali, non committarlo.")

    if args.xlsx:
        try:
            from xlsx_export import write_xlsx
        except ImportError as e:
            print(f"\nERRORE: export XLSX non disponibile ({e}). Serve: pip install openpyxl")
            sys.exit(1)
        write_xlsx(users, args.xlsx)
        print(f"\nXLSX scritto: {args.xlsx} — contiene dati personali, non committarlo.")


if __name__ == "__main__":
    main()
