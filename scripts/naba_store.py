#!/usr/bin/env python3
"""
naba_store.py

Client per il Worker Cloudflare che fa da storage dei dati utente.
Sostituisce il vecchio CSV committato nella repo: nessun dato personale
tocca più il filesystem del progetto.

Usato da perforce_provision.py, export_p4_users.py, kv_status.py e
discord_email_provision.py.

Il token admin si prende, in ordine:
  1. variabile d'ambiente NABA_ADMIN_TOKEN
  2. prompt interattivo con getpass

L'URL del Worker si può sovrascrivere con NABA_WORKER_URL.
"""

import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


# ══════════════════════════════════════════════════════════════
# CONFIGURAZIONE
# ══════════════════════════════════════════════════════════════
DEFAULT_WORKER_URL = "https://perforce-registration.tech-0a4.workers.dev"

FIELDS = [
    "timestamp", "username", "full_name", "email",
    "team", "tesista", "anno_corso", "status",
]

VALID_STATUS = {"pending", "created", "existing", "removed", "duplicate", "error"}

USER_AGENT = "NABA-Perforce-Admin/1.0"
TIMEOUT = 30
# ══════════════════════════════════════════════════════════════


class StoreError(RuntimeError):
    """Errore di comunicazione con il Worker."""


def worker_url() -> str:
    return os.environ.get("NABA_WORKER_URL", DEFAULT_WORKER_URL).rstrip("/")


def get_admin_token(prompt: str = "Admin token del Worker: ") -> str:
    """Token da env o da prompt nascosto. Non viene mai stampato né salvato."""
    token = os.environ.get("NABA_ADMIN_TOKEN", "").strip()
    if token:
        return token

    token = getpass.getpass(prompt).strip()
    if not token:
        print("ERRORE: nessun token inserito.")
        sys.exit(1)
    return token


# ── Log anonimizzati ───────────────────────────────────────────
def mask_email(email: str) -> str:
    """mario.rossi@studenti.naba.it → m***@studenti.naba.it"""
    email = (email or "").strip()
    if "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if local else f"***@{domain}"


# ── HTTP ───────────────────────────────────────────────────────
def _request(method: str, path: str, token: str, body=None, params: dict = None) -> dict:
    url = worker_url() + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v})

    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "X-Admin-Token": token,
        "User-Agent": USER_AGENT,
    }
    if data is not None:
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(detail).get("error", detail)
        except json.JSONDecodeError:
            pass
        # I due errori tipici di un Worker appena deployato.
        if e.code == 401:
            raise StoreError(
                "token admin rifiutato (401). Verifica che il secret ADMIN_TOKEN "
                "sul Worker sia lo stesso che stai inserendo."
            ) from e
        if e.code == 500 and "non configurato" in detail:
            raise StoreError(
                "il Worker non trova il KV (500). Verifica che il binding si chiami "
                "esattamente USERS in Settings → Bindings."
            ) from e
        raise StoreError(f"{method} {path} → HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise StoreError(f"Worker non raggiungibile ({worker_url()}): {e.reason}") from e

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise StoreError(f"{method} {path} → risposta non JSON") from e


# ── API ────────────────────────────────────────────────────────
def fetch_users(token: str, status: str = None) -> list[dict]:
    """Scarica tutti i record dal KV, opzionalmente filtrati per status."""
    result = _request("GET", "/export", token, params={"status": status})
    if not result.get("success"):
        raise StoreError(f"export fallito: {result.get('error', 'errore sconosciuto')}")

    users = result.get("users", [])
    # Difensivo: garantisce che ogni record abbia tutte le colonne attese.
    return [{f: u.get(f, "") for f in FIELDS} for u in users]


def import_users(
    token: str,
    users: list[dict],
    skip_existing: bool = False,
    default_status: str = None,
    chunk_size: int = 200,
) -> dict:
    """
    Upsert di record nel KV, a blocchi per non superare il limite del Worker.
    Ritorna il totale scritti/saltati e gli eventuali record rifiutati.
    """
    if not users:
        return {"written": 0, "skipped": 0, "rejected": []}

    params = {}
    if skip_existing:
        params["mode"] = "skip-existing"
    if default_status:
        params["default_status"] = default_status

    written = skipped = 0
    rejected = []

    for i in range(0, len(users), chunk_size):
        chunk = users[i:i + chunk_size]
        result = _request("POST", "/import", token, body={"users": chunk}, params=params)
        if not result.get("success"):
            raise StoreError(f"import fallito: {result.get('error', 'errore sconosciuto')}")

        written += result.get("written", 0)
        skipped += result.get("skipped", 0)
        rejected += [r for r in result.get("results", []) if not r.get("ok")]

    return {"written": written, "skipped": skipped, "rejected": rejected}


def patch_status(token: str, updates: list[dict], chunk_size: int = 200) -> dict:
    """
    Aggiorna lo status di uno o più utenti.
    Ogni update è {"username": ..., "status": ..., "team": ...}.
    Senza 'team' aggiorna tutti i record dell'utente.
    """
    if not updates:
        return {"updated": 0, "failed": []}

    for u in updates:
        if u.get("status") not in VALID_STATUS:
            raise ValueError(f"status non valido: {u.get('status')!r}")

    updated = 0
    failed = []

    for i in range(0, len(updates), chunk_size):
        chunk = updates[i:i + chunk_size]
        result = _request("PATCH", "/status", token, body={"updates": chunk})
        if not result.get("success"):
            raise StoreError(f"patch status fallito: {result.get('error', 'errore sconosciuto')}")

        updated += result.get("updated", 0)
        failed += [r for r in result.get("results", []) if not r.get("ok")]

    return {"updated": updated, "failed": failed}


def delete_user(token: str, username: str, team: str = None) -> int:
    """
    Rimuove i record di un utente. Senza `team` li rimuove tutti.
    Ritorna quanti record sono stati cancellati.
    """
    params = {"username": username}
    if team:
        params["team"] = team

    result = _request("DELETE", "/user", token, params=params)
    if not result.get("success"):
        raise StoreError(f"delete fallito: {result.get('error', 'errore sconosciuto')}")
    return result.get("deleted", 0)


def purge(token: str) -> int:
    """Svuota il KV. Irreversibile."""
    result = _request("DELETE", "/purge", token, params={"confirm": "CONFIRM"})
    if not result.get("success"):
        raise StoreError(f"purge fallito: {result.get('error', 'errore sconosciuto')}")
    return result.get("deleted", 0)


def wait_for_count(token: str, expected: int, attempts: int = 6, delay: float = 5.0) -> int:
    """
    Il KV è eventually consistent: dopo una scrittura la lista può restare
    indietro di qualche decina di secondi. Rilegge finché il conteggio
    non arriva almeno a `expected`.
    """
    count = 0
    for attempt in range(1, attempts + 1):
        count = len(fetch_users(token))
        if count >= expected:
            return count
        if attempt < attempts:
            print(f"  KV non ancora allineato ({count}/{expected}), riprovo tra {delay:.0f}s...")
            time.sleep(delay)
    return count
