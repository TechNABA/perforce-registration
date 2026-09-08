#!/usr/bin/env python3
"""
test_scripts.py

Test degli script admin. Coprono solo logica pura o funzioni con un finto
client davanti: nessun test tocca il server Perforce o il KV Cloudflare.

    python -m unittest discover -s test

Sono la rete di sicurezza attorno a tre cose che, se si rompono, si rompono in
silenzio: la costruzione dei comandi p4, la riscrittura degli spec dei gruppi,
e le guardie che impediscono di cancellare l'account con cui sei connesso.
"""

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import p4_common as p4c
import perforce_cleanup


def completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=["p4"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


class FakeP4:
    """Client finto: registra le chiamate e restituisce risposte preparate."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []
        self.user = "admin"
        self.port = "server:1666"

    def run(self, *args, stdin_text=None):
        self.calls.append((args, stdin_text))
        if self.responses:
            return self.responses.pop(0)
        return completed()


# ── Validazione dei nomi ────────────────────────────────────────
class TestNameValidation(unittest.TestCase):
    def test_accetta_gli_username_che_genera_il_form(self):
        for name in ["mario_rossi", "ProjectAlpha", "a.b-c_1", "ZZTest"]:
            self.assertTrue(p4c.valid_p4_name(name), name)

    def test_rifiuta_i_metacaratteri_di_shell(self):
        # È il caso che rendeva sfruttabile shell=True.
        for name in ["Alfa & calc", "a;rm -rf /", "a|b", "a`id`", "a$(id)",
                     "a b", "a\nb", "a\tb", "", "   ", "../etc"]:
            self.assertFalse(p4c.valid_p4_name(name), repr(name))

    def test_check_p4_name_pulisce_gli_spazi_ai_bordi(self):
        self.assertEqual(p4c.check_p4_name("  mario_rossi  ", "username"), "mario_rossi")

    def test_check_p4_name_solleva_su_valore_non_valido(self):
        with self.assertRaises(p4c.P4Error):
            p4c.check_p4_name("Alfa & calc", "team")

    def test_clean_spec_value_neutralizza_tab_e_a_capo(self):
        # Con questi due caratteri si iniettano campi in uno spec Perforce.
        sporco = "Mario\tRossi\nUsers:\n\tadmin"
        self.assertEqual(p4c.clean_spec_value(sporco), "Mario Rossi Users: admin")


# ── Costruzione dei comandi ─────────────────────────────────────
class TestP4Client(unittest.TestCase):
    def test_run_passa_una_lista_e_non_usa_la_shell(self):
        client = p4c.P4Client("server:1666", "admin", "segreta")
        with mock.patch.object(p4c.subprocess, "run", return_value=completed()) as run:
            client.run("group", "-o", "Alfa & calc")

        args, kwargs = run.call_args
        self.assertEqual(args[0], ["p4", "group", "-o", "Alfa & calc"])
        self.assertFalse(kwargs["shell"])

    def test_env_porta_le_credenziali_al_sottoprocesso(self):
        client = p4c.P4Client("server:1666", "admin", "segreta")
        env = client.env()
        self.assertEqual(env["P4PORT"], "server:1666")
        self.assertEqual(env["P4USER"], "admin")
        self.assertEqual(env["P4PASSWD"], "segreta")

    def test_env_senza_password_non_imposta_p4passwd(self):
        self.assertNotIn("P4PASSWD", p4c.P4Client("s:1666", "admin").env())


# ── Spec dei gruppi ─────────────────────────────────────────────
GROUP_SPEC = (
    "Group:\tAlfa\n"
    "MaxResults:\tunset\n"
    "Timeout:\t43200\n"
    "Users:\n"
    "\tmario_rossi\n"
    "\tmario_rossini\n"
    "\tanna_bianchi\n"
)


class TestGroupSpec(unittest.TestCase):
    def test_parse_group_members(self):
        self.assertEqual(
            p4c.parse_group_members(GROUP_SPEC),
            ["mario_rossi", "mario_rossini", "anna_bianchi"],
        )

    def test_toglie_solo_lutente_indicato(self):
        nuovo, trovato, rimasti = p4c.strip_user_from_group_spec(GROUP_SPEC, "mario_rossi")
        self.assertTrue(trovato)
        self.assertEqual(rimasti, 2)
        # Il collega col nome più lungo non deve essere trascinato via.
        self.assertEqual(p4c.parse_group_members(nuovo), ["mario_rossini", "anna_bianchi"])

    def test_utente_assente_non_cambia_nulla(self):
        nuovo, trovato, rimasti = p4c.strip_user_from_group_spec(GROUP_SPEC, "carlo_verdi")
        self.assertFalse(trovato)
        self.assertEqual(rimasti, 3)
        self.assertEqual(p4c.parse_group_members(nuovo),
                         ["mario_rossi", "mario_rossini", "anna_bianchi"])

    def test_ultimo_membro_segnala_zero_rimasti(self):
        spec = "Group:\tAlfa\nUsers:\n\tmario_rossi\n"
        nuovo, trovato, rimasti = p4c.strip_user_from_group_spec(spec, "mario_rossi")
        self.assertTrue(trovato)
        self.assertEqual(rimasti, 0)
        self.assertEqual(p4c.parse_group_members(nuovo), [])

    def test_gruppo_senza_sezione_users(self):
        spec = "Group:\tAlfa\nTimeout:\t43200\n"
        nuovo, trovato, rimasti = p4c.strip_user_from_group_spec(spec, "mario_rossi")
        self.assertFalse(trovato)
        self.assertEqual(rimasti, 0)
        self.assertIn("Group:", nuovo)

    def test_aggiunge_un_utente(self):
        spec = "Group:\tAlfa\nUsers:\n\tanna_bianchi\n"
        nuovo, gia_presente = p4c.add_user_to_group_spec(spec, "mario_rossi")
        self.assertFalse(gia_presente)
        self.assertEqual(p4c.parse_group_members(nuovo), ["mario_rossi", "anna_bianchi"])

    def test_aggiunta_idempotente(self):
        nuovo, gia_presente = p4c.add_user_to_group_spec(GROUP_SPEC, "mario_rossi")
        self.assertTrue(gia_presente)
        self.assertEqual(nuovo, GROUP_SPEC)

    def test_aggiunge_la_sezione_users_se_manca(self):
        nuovo, gia_presente = p4c.add_user_to_group_spec("Group:\tAlfa\n", "mario_rossi")
        self.assertFalse(gia_presente)
        self.assertEqual(p4c.parse_group_members(nuovo), ["mario_rossi"])


class TestViewDepots(unittest.TestCase):
    def test_estrae_i_depot_dalla_view(self):
        spec = (
            "Client:\tws_mario\n"
            "View:\n"
            "\t//Alfa/... //ws_mario/Alfa/...\n"
            "\t-//Beta/segreto/... //ws_mario/Beta/segreto/...\n"
        )
        self.assertEqual(p4c.parse_view_depots(spec), {"Alfa", "Beta"})

    def test_senza_view_nessun_depot(self):
        self.assertEqual(p4c.parse_view_depots("Client:\tws\n"), set())


# ── Operazioni distruttive ──────────────────────────────────────
class TestDestructive(unittest.TestCase):
    def test_dry_run_non_chiama_il_server(self):
        client = FakeP4()
        for ok, _ in (
            p4c.delete_workspace(client, "ws", dry_run=True),
            p4c.delete_pending_change(client, "42", dry_run=True),
            p4c.delete_user(client, "mario_rossi", dry_run=True),
        ):
            self.assertTrue(ok)
        self.assertEqual(client.calls, [])

    def test_workspace_non_cancellato_se_il_revert_fallisce(self):
        # Se il revert non riesce, il client non va cancellato lo stesso.
        client = FakeP4([completed(returncode=1, stderr="file lockato")])
        ok, err = p4c.delete_workspace(client, "ws_mario")
        self.assertFalse(ok)
        self.assertIn("revert", err)
        self.assertEqual(len(client.calls), 1)

    def test_changelist_non_cancellata_se_il_revert_fallisce(self):
        client = FakeP4([completed(returncode=1, stderr="in uso")])
        ok, err = p4c.delete_pending_change(client, "42")
        self.assertFalse(ok)
        self.assertEqual(len(client.calls), 1)

    def test_workspace_cancellato_dopo_un_revert_riuscito(self):
        client = FakeP4([completed(), completed()])
        ok, err = p4c.delete_workspace(client, "ws_mario")
        self.assertTrue(ok)
        self.assertEqual(err, "")
        self.assertEqual(client.calls[0][0], ("-c", "ws_mario", "revert", "//..."))
        self.assertEqual(client.calls[1][0], ("client", "-d", "-f", "ws_mario"))

    def test_remove_user_from_group_scrive_lo_spec_ripulito(self):
        client = FakeP4([completed(stdout=GROUP_SPEC), completed()])
        ok, err, era_membro, rimasti = p4c.remove_user_from_group(
            client, "mario_rossi", "Alfa"
        )
        self.assertTrue(ok)
        self.assertTrue(era_membro)
        self.assertEqual(rimasti, 2)
        scritto = client.calls[1][1]
        self.assertNotIn("\tmario_rossi\n", scritto)
        self.assertIn("\tmario_rossini\n", scritto)

    def test_remove_user_from_group_non_scrive_se_non_e_membro(self):
        client = FakeP4([completed(stdout=GROUP_SPEC)])
        ok, err, era_membro, _ = p4c.remove_user_from_group(client, "carlo_verdi", "Alfa")
        self.assertTrue(ok)
        self.assertFalse(era_membro)
        self.assertEqual(len(client.calls), 1)  # nessuna scrittura

    def test_remove_user_from_group_in_dry_run_non_scrive(self):
        client = FakeP4([completed(stdout=GROUP_SPEC)])
        ok, _, era_membro, _ = p4c.remove_user_from_group(
            client, "mario_rossi", "Alfa", dry_run=True
        )
        self.assertTrue(ok)
        self.assertTrue(era_membro)
        self.assertEqual(len(client.calls), 1)


# ── Prompt di connessione ───────────────────────────────────────
class TestAskConnection(unittest.TestCase):
    def test_server_vuoto_ferma_tutto(self):
        with mock.patch("builtins.input", return_value=""):
            with self.assertRaises(SystemExit):
                p4c.ask_p4_connection()

    def test_utente_vuoto_ferma_tutto(self):
        with mock.patch("builtins.input", side_effect=["server:1666", ""]):
            with self.assertRaises(SystemExit):
                p4c.ask_p4_connection()

    def test_ordine_server_utente_password(self):
        with mock.patch("builtins.input", side_effect=["10.0.0.5:1666", "admin"]):
            with mock.patch.object(p4c.getpass, "getpass", return_value="segreta"):
                client = p4c.ask_p4_connection()
        self.assertEqual(client.port, "10.0.0.5:1666")
        self.assertEqual(client.user, "admin")
        self.assertEqual(client.password, "segreta")


# ── Filtro dei record sul KV ────────────────────────────────────
ROWS = [
    {"username": "mario_rossi", "team": "Alfa", "status": "created"},
    {"username": "Mario_Rossi", "team": " Beta ", "status": "existing"},
    {"username": "anna_bianchi", "team": "Alfa", "status": "created"},
]


class TestKvMatches(unittest.TestCase):
    def test_match_case_insensitive_su_tutti_i_team(self):
        trovati = perforce_cleanup.find_kv_matches(ROWS, "MARIO_ROSSI")
        self.assertEqual(len(trovati), 2)

    def test_filtro_per_team_ignora_maiuscole_e_spazi(self):
        trovati = perforce_cleanup.find_kv_matches(ROWS, "mario_rossi", "beta")
        self.assertEqual(len(trovati), 1)
        self.assertEqual(trovati[0]["team"], " Beta ")

    def test_nessun_match(self):
        self.assertEqual(perforce_cleanup.find_kv_matches(ROWS, "carlo_verdi"), [])

    def test_team_inesistente_non_restituisce_nulla(self):
        self.assertEqual(perforce_cleanup.find_kv_matches(ROWS, "mario_rossi", "Gamma"), [])


# ── purge() non si autoconferma ─────────────────────────────────
class TestPurgeConfirm(unittest.TestCase):
    def test_purge_senza_conferma_esplicita_non_parte(self):
        import naba_store
        with mock.patch.object(naba_store, "_request") as req:
            with self.assertRaises(naba_store.StoreError):
                naba_store.purge("token", "")
        req.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
