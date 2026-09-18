#!/usr/bin/env python3
"""
p4_common.py

Quello che tutti gli script Perforce devono fare allo stesso modo: aprire la
connessione, lanciare comandi `p4`, e cancellare utenti, workspace e changelist.

La regola che conta è che i comandi si costruiscono come lista di argomenti e
non come stringa passata alla shell. Username e nomi di team arrivano dal form
pubblico e i nomi dei workspace li sceglie lo studente: con `shell=True` una `&`
in un nome di team basta a eseguire comandi arbitrari con le credenziali
dell'operatore, e il payload resta latente finché un altro script rilegge quel
gruppo. Per lo stesso motivo i valori che finiscono dentro uno spec Perforce
passano da clean_spec_value(): un tab o un a capo spezzerebbero lo spec e
permetterebbero di iniettare campi che non abbiamo scritto noi.

Le funzioni che cancellano stanno qui e non nei singoli script perché erano
duplicate fra perforce_cleanup.py e perforce_prune.py e avevano già iniziato a
divergere.
"""

import getpass
import os
import re
import subprocess
import sys


# Username, gruppi, depot e workspace. Perforce accetterebbe di più, ma questo
# è tutto ciò che il nostro flusso genera: quello che non rientra è sospetto.
P4_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class P4Error(RuntimeError):
    """Valore rifiutato prima ancora di arrivare al server."""


def valid_p4_name(name: str) -> bool:
    return bool(P4_NAME_RE.match((name or "").strip()))


def check_p4_name(name: str, what: str = "nome") -> str:
    """Ritorna il nome ripulito, o solleva P4Error se non è accettabile."""
    clean = (name or "").strip()
    if not valid_p4_name(clean):
        raise P4Error(
            f"{what} non valido: {clean!r} — il primo carattere deve essere "
            f"lettera o cifra, poi solo lettere, cifre, punto, trattino e underscore"
        )
    return clean


def clean_spec_value(value: str) -> str:
    """
    Un campo di testo libero (nome completo, email) che va dentro uno spec.
    Tab e a capo diventano spazi singoli: sono i due caratteri con cui si
    iniettano campi aggiuntivi in uno spec Perforce.
    """
    return " ".join((value or "").split())


# ── Connessione ─────────────────────────────────────────────────
class P4Client:
    """
    Una connessione Perforce. Gli script ne tengono una sola, ma passarla come
    parametro invece di leggerla da una globale rende testabili le funzioni che
    cancellano dati, senza un server vero davanti.
    """

    def __init__(self, port: str, user: str, password: str = ""):
        self.port = port
        self.user = user
        self.password = password

    def env(self) -> dict:
        env = os.environ.copy()
        env["P4PORT"] = self.port
        env["P4USER"] = self.user
        if self.password:
            env["P4PASSWD"] = self.password
        return env

    def run(self, *args: str, stdin_text: str = None) -> subprocess.CompletedProcess:
        """Esegue `p4` con gli argomenti come lista: nessuna shell di mezzo."""
        return subprocess.run(
            ["p4", *[str(a) for a in args]],
            shell=False,
            capture_output=True,
            text=True,
            input=stdin_text,
            env=self.env(),
        )


def ask_p4_connection() -> P4Client:
    """
    Chiede server, utente e password, in quest'ordine, a ogni esecuzione.
    L'indirizzo cambia a seconda della rete da cui si lavora, quindi non ha un
    default: dalla VLAN del virtual studio il server va indicato per IP.
    """
    port = input("Server Perforce (host:porta): ").strip()
    if not port:
        print("ERRORE: serve l'indirizzo del server.")
        sys.exit(1)

    user = input("Utente Perforce: ").strip()
    if not user:
        print("ERRORE: serve l'utente.")
        sys.exit(1)

    password = getpass.getpass(f"Password per {user}: ")
    return P4Client(port, user, password)


def connect(client: P4Client) -> subprocess.CompletedProcess:
    """`p4 info`, usato dagli script per verificare che la connessione regga."""
    return client.run("info")


# ── Spec dei gruppi: logica pura, testabile senza server ────────
def parse_group_members(spec_text: str) -> list[str]:
    """Gli username nella sezione Users: di uno spec di gruppo."""
    members = []
    in_users = False
    for line in spec_text.split("\n"):
        if line.startswith("Users:"):
            in_users = True
            continue
        if in_users:
            if line.startswith("\t"):
                member = line.strip()
                if member:
                    members.append(member)
            elif line.strip():
                break
    return members


def strip_user_from_group_spec(spec_text: str, username: str) -> tuple[str, bool, int]:
    """
    Toglie l'utente dalla sezione Users: di uno spec di gruppo.
    Ritorna (nuovo spec, trovato, quanti membri restano).

    Il confronto è esatto: un utente il cui nome è prefisso di un altro non
    deve trascinarsi via il collega.
    """
    new_lines = []
    in_users = False
    found = False
    remaining = 0

    for line in spec_text.strip().split("\n"):
        if line.startswith("Users:"):
            in_users = True
            new_lines.append(line)
            continue

        if in_users:
            if line.startswith("\t"):
                if line.strip() == username:
                    found = True
                    continue  # la riga dell'utente non viene ricopiata
                if line.strip():
                    remaining += 1
            elif line.strip():
                in_users = False

        new_lines.append(line)

    return "\n".join(new_lines) + "\n", found, remaining


def add_user_to_group_spec(spec_text: str, username: str) -> tuple[str, bool]:
    """
    Aggiunge l'utente alla sezione Users: di uno spec di gruppo.
    Ritorna (nuovo spec, era già presente).
    """
    if username in parse_group_members(spec_text):
        return spec_text, True

    new_lines = []
    users_found = False

    for line in spec_text.strip().split("\n"):
        new_lines.append(line)
        if line.startswith("Users:"):
            users_found = True
            new_lines.append(f"\t{username}")

    if not users_found:
        new_lines.append("Users:")
        new_lines.append(f"\t{username}")

    return "\n".join(new_lines) + "\n", False


def parse_view_depots(spec_text: str) -> set[str]:
    """I depot mappati nella View di uno spec di workspace."""
    depots = set()
    in_view = False
    for line in spec_text.split("\n"):
        if line.startswith("View:"):
            in_view = True
            continue
        if in_view:
            if line.startswith("\t") or line.startswith("    "):
                entry = line.strip().lstrip("-+").strip('"').lstrip("&")
                if entry.startswith("//"):
                    depot = entry[2:].split("/")[0]
                    if depot:
                        depots.add(depot)
            elif line.strip():
                break
    return depots


# ── Letture ─────────────────────────────────────────────────────
def user_exists(client: P4Client, username: str) -> bool:
    return username in client.run("users", username).stdout


def user_groups(client: P4Client, username: str) -> list[str]:
    """Gruppi di cui l'utente è membro diretto."""
    result = client.run("groups", username)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]


def user_workspaces(client: P4Client, username: str) -> list[str]:
    result = client.run("clients", "-u", username)
    workspaces = []
    for line in result.stdout.strip().split("\n"):
        if line.startswith("Client "):
            workspaces.append(line.split(" ")[1])
    return workspaces


def workspace_depots(client: P4Client, ws_name: str) -> set[str]:
    """I depot su cui un workspace è mappato."""
    result = client.run("client", "-o", ws_name)
    if result.returncode != 0:
        return set()
    return parse_view_depots(result.stdout)


def change_client(client: P4Client, change: str) -> str:
    """Il workspace a cui appartiene una changelist."""
    result = client.run("change", "-o", change)
    if result.returncode != 0:
        return ""
    for line in result.stdout.split("\n"):
        if line.startswith("Client:"):
            return line.split(":", 1)[-1].strip()
    return ""


def pending_changes(client: P4Client, username: str) -> list[str]:
    """Changelist pending dell'utente. Bloccano la cancellazione dell'account."""
    result = client.run("changes", "-u", username, "-s", "pending")
    changes = []
    for line in result.stdout.strip().split("\n"):
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "Change":
            changes.append(parts[1])
    return changes


# ── Scritture distruttive ───────────────────────────────────────
def delete_pending_change(client: P4Client, change: str,
                          dry_run: bool = False) -> tuple[bool, str]:
    """
    Cancella una changelist pending, rilasciando prima i file aperti.
    Ritorna (riuscito, messaggio d'errore).
    """
    if dry_run:
        return True, ""

    ws_name = change_client(client, change)
    if not ws_name:
        return False, f"changelist {change}: workspace non trovato, revert impossibile"

    # Senza il revert la changelist non si lascia cancellare: i file restano aperti.
    # -C è il client, -c la changelist: revert come admin nel workspace di un
    # altro utente, non richiede di essere quel client.
    reverted = client.run("revert", "-C", ws_name, "-c", change, "//...")
    if reverted.returncode != 0:
        return False, f"revert della changelist {change} fallito: {reverted.stderr.strip()}"

    result = client.run("change", "-d", "-f", change)
    if result.returncode != 0:
        return False, result.stderr.strip()
    return True, ""


def delete_workspace(client: P4Client, ws_name: str,
                     dry_run: bool = False) -> tuple[bool, str]:
    """Cancella un workspace, rilasciandone prima i file aperti."""
    if dry_run:
        return True, ""

    # Forma admin: -C è il client, non "-c ws revert" (quella richiede di
    # essere quel client, non lo revertirebbe per un altro).
    reverted = client.run("revert", "-C", ws_name, "//...")
    if reverted.returncode != 0:
        return False, f"revert del workspace '{ws_name}' fallito: {reverted.stderr.strip()}"

    result = client.run("client", "-d", "-f", ws_name)
    if result.returncode != 0:
        return False, result.stderr.strip()
    return True, ""


def delete_user(client: P4Client, username: str,
                dry_run: bool = False) -> tuple[bool, str]:
    """Cancella l'account Perforce."""
    if dry_run:
        return True, ""

    result = client.run("user", "-d", "-f", username)
    if result.returncode != 0:
        return False, result.stderr.strip()
    return True, ""


def remove_user_from_group(client: P4Client, username: str, group_name: str,
                           dry_run: bool = False) -> tuple[bool, str, bool, int]:
    """
    Toglie l'utente dalla sezione Users: dello spec del gruppo.
    Ritorna (riuscito, errore, era membro, membri rimasti).

    Se era l'ultimo membro Perforce cancella il gruppo. Depot e protezioni
    restano, e va bene così: il gruppo si ricrea al prossimo provisioning.
    """
    result = client.run("group", "-o", group_name)
    if result.returncode != 0:
        return False, f"gruppo non leggibile: {result.stderr.strip()}", False, 0

    new_spec, found, remaining = strip_user_from_group_spec(result.stdout, username)

    if not found:
        return True, "", False, remaining

    if dry_run:
        return True, "", True, remaining

    written = client.run("group", "-i", stdin_text=new_spec)
    if written.returncode != 0:
        return False, written.stderr.strip(), True, remaining

    return True, "", True, remaining
