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

import contextlib
import io
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import export_p4_users
import p4_common as p4c
import perforce_cleanup
import perforce_prune
import perforce_provision


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

    def test_rifiuta_i_nomi_che_iniziano_con_un_trattino(self):
        # "-D" sembrerebbe un'opzione a p4.
        for name in ["-D", "-", "-mario"]:
            self.assertFalse(p4c.valid_p4_name(name), repr(name))

    def test_rifiuta_i_nomi_che_iniziano_con_un_punto(self):
        # Il nome del team diventa anche il Map: del depot: "../..." punterebbe
        # fuori dalla root del server.
        for name in [".hidden", ".", "..", "..."]:
            self.assertFalse(p4c.valid_p4_name(name), repr(name))

    def test_accetta_i_nomi_con_underscore_iniziale(self):
        # Il vecchio form generava "_wang" quando il nome non aveva lettere
        # latine: quei record esistono e vanno ancora creati e ripuliti.
        self.assertTrue(p4c.valid_p4_name("_wang"))

    def test_rifiuta_i_nomi_solo_numerici(self):
        # p4 non accetta nomi fatti solo di cifre: il record passerebbe dal
        # form e fallirebbe solo al provisioning, senza avvisare nessuno.
        for name in ["2024", "0", "12345"]:
            self.assertFalse(p4c.valid_p4_name(name), repr(name))

    def test_rifiuta_i_nomi_con_tre_punti(self):
        # "..." è il jolly di p4: nella protezione "//a.../..." varrebbe per
        # tutti i depot che iniziano con "a".
        for name in ["a...", "a...b", "_...", "a....b"]:
            self.assertFalse(p4c.valid_p4_name(name), repr(name))

    def test_accetta_cifre_con_altro_e_punti_non_tripli(self):
        for name in ["2024a", "Team2024", "_2024", "a..b", "a.b.c"]:
            self.assertTrue(p4c.valid_p4_name(name), repr(name))

    def test_check_p4_name_solleva_se_il_nome_inizia_con_un_trattino(self):
        with self.assertRaises(p4c.P4Error):
            p4c.check_p4_name("-D", "team")

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

    def test_mapping_ditto_conta_come_mapping_del_depot(self):
        # "&//Depot/..." è la forma ditto della View: mappa Depot esattamente
        # come "-//" e "+//".
        spec = (
            "Client:\tws_mario\n"
            "View:\n"
            "\t&//Alfa/... //ws_mario/Alfa/...\n"
        )
        self.assertEqual(p4c.parse_view_depots(spec), {"Alfa"})

    def test_mapping_ditto_tra_virgolette_conta_come_mapping_del_depot(self):
        spec = (
            "Client:\tws_mario\n"
            "View:\n"
            "\t\"&//Alfa/...\" \"//ws_mario/Alfa/...\"\n"
        )
        self.assertEqual(p4c.parse_view_depots(spec), {"Alfa"})

    def test_prefisso_dentro_le_virgolette_conta_come_mapping_del_depot(self):
        # Con uno spazio nel path la riga è tra virgolette e il +/- sta dentro.
        spec = (
            "Client:\tws_mario\n"
            "View:\n"
            "\t//Alfa/... //ws_mario/Alfa/...\n"
            "\t\"+//Beta/My Assets/...\" \"//ws_mario/My Assets/...\"\n"
            "\t\"-//Gamma/a b/...\" \"//ws_mario/a b/...\"\n"
        )
        self.assertEqual(p4c.parse_view_depots(spec), {"Alfa", "Beta", "Gamma"})

    def test_prefisso_fuori_dalle_virgolette_conta_come_mapping_del_depot(self):
        spec = (
            "Client:\tws_mario\n"
            "View:\n"
            "\t-\"//Beta/a b/...\" \"//ws_mario/a b/...\"\n"
        )
        self.assertEqual(p4c.parse_view_depots(spec), {"Beta"})


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
        # Trova il workspace giusto (change -o), ma il revert in quel
        # workspace fallisce: niente change -d -f.
        change_spec = "Change:\t42\nClient:\tws_mario\nStatus:\tpending\n"
        client = FakeP4([completed(stdout=change_spec),
                         completed(returncode=1, stderr="boom")])
        ok, err = p4c.delete_pending_change(client, "42")
        self.assertEqual((ok, err), (False, "revert della changelist 42 fallito: boom"))
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[1][0], ("revert", "-C", "ws_mario", "-c", "42", "//..."))

    def test_workspace_cancellato_dopo_un_revert_riuscito(self):
        # Forma admin: -C è il client, non "-c client revert" (quella
        # richiede che l'operatore SIA quel client, non lo cancella per altri).
        client = FakeP4([completed(), completed()])
        ok, err = p4c.delete_workspace(client, "ws_mario")
        self.assertTrue(ok)
        self.assertEqual(err, "")
        self.assertEqual(client.calls[0][0], ("revert", "-C", "ws_mario", "//..."))
        self.assertEqual(client.calls[1][0], ("client", "-d", "-f", "ws_mario"))

    def test_changelist_riverte_nel_workspace_della_changelist_e_poi_la_cancella(self):
        # delete_pending_change deve prima ricavare il workspace con
        # change_client (change -o), poi revertare in forma admin (-C <ws> -c
        # <change>), non trattare `change` come se fosse un nome di workspace.
        change_spec = "Change:\t42\nClient:\tws_mario\nStatus:\tpending\n"
        client = FakeP4([completed(stdout=change_spec), completed(), completed(), completed()])
        ok, err = p4c.delete_pending_change(client, "42")
        self.assertTrue(ok)
        self.assertEqual(err, "")
        self.assertEqual(client.calls[0][0], ("change", "-o", "42"))
        self.assertEqual(client.calls[1][0], ("revert", "-C", "ws_mario", "-c", "42", "//..."))
        self.assertEqual(client.calls[2][0], ("shelve", "-d", "-f", "-c", "42"))
        self.assertEqual(client.calls[3][0], ("change", "-d", "-f", "42"))

    def test_changelist_senza_shelve_si_cancella_anche_se_shelve_d_protesta(self):
        # Senza file in shelve `shelve -d` fallisce: non conta, conta `change -d`.
        change_spec = "Change:\t42\nClient:\tws_mario\nStatus:\tpending\n"
        client = FakeP4([completed(stdout=change_spec), completed(),
                         completed(returncode=1, stderr="No shelved files in changelist to delete."),
                         completed()])
        ok, err = p4c.delete_pending_change(client, "42")
        self.assertEqual((ok, err), (True, ""))
        self.assertEqual(client.calls[3][0], ("change", "-d", "-f", "42"))

    def test_se_lo_shelve_resta_lerrore_lo_riporta(self):
        # Shelve non cancellabile (es. resolve pendenti di un altro utente):
        # `change -d` fallisce e l'operatore deve vedere anche il perché.
        change_spec = "Change:\t42\nClient:\tws_mario\nStatus:\tpending\n"
        client = FakeP4([completed(stdout=change_spec), completed(),
                         completed(returncode=1, stderr="pending resolves"),
                         completed(returncode=1, stderr="Change 42 has shelved files")])
        ok, err = p4c.delete_pending_change(client, "42")
        self.assertFalse(ok)
        self.assertIn("Change 42 has shelved files", err)
        self.assertIn("pending resolves", err)

    def test_changelist_senza_workspace_non_chiama_revert(self):
        # Se change -o non riporta un Client:, non c'è un client per il -C:
        # niente revert, niente change -d.
        client = FakeP4([completed(stdout="Change:\t42\nStatus:\tpending\n")])
        ok, err = p4c.delete_pending_change(client, "42")
        self.assertFalse(ok)
        self.assertIn("workspace non trovato", err)
        self.assertEqual(len(client.calls), 1)

    def test_changelist_non_cancellata_se_change_d_fallisce_dopo_un_revert_riuscito(self):
        # Revert riuscito, ma "change -d -f" fallisce (es. bloccata da un lock):
        # l'errore del server va propagato così com'è, non inghiottito.
        change_spec = "Change:\t42\nClient:\tws_mario\nStatus:\tpending\n"
        client = FakeP4([completed(stdout=change_spec), completed(), completed(),
                         completed(returncode=1, stderr="locked")])
        ok, err = p4c.delete_pending_change(client, "42")
        self.assertEqual((ok, err), (False, "locked"))
        self.assertEqual(len(client.calls), 4)

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


# ── select_team_workspaces (contratto 2a) ────────────────────────
class TestSelectTeamWorkspaces(unittest.TestCase):
    def test_workspace_con_solo_il_depot_del_team_e_targeted(self):
        spec = (
            "Client:\tws1\n"
            "View:\n"
            "\t//Alfa/... //ws1/Alfa/...\n"
        )
        client = FakeP4([completed(stdout=spec)])
        targeted, shared, unreadable = perforce_cleanup.select_team_workspaces(client, ["ws1"], "alfa")
        self.assertEqual(targeted, ["ws1"])
        self.assertEqual(shared, [])

    def test_workspace_con_anche_un_altro_depot_e_shared_non_targeted(self):
        spec = (
            "Client:\tws2\n"
            "View:\n"
            "\t//Alfa/... //ws2/Alfa/...\n"
            "\t//Beta/... //ws2/Beta/...\n"
        )
        client = FakeP4([completed(stdout=spec)])
        targeted, shared, unreadable = perforce_cleanup.select_team_workspaces(client, ["ws2"], "alfa")
        self.assertEqual(targeted, [])
        self.assertEqual(shared, ["ws2"])

    def test_workspace_senza_il_depot_del_team_non_compare_in_nessuna_lista(self):
        spec = (
            "Client:\tws3\n"
            "View:\n"
            "\t//Beta/... //ws3/Beta/...\n"
        )
        client = FakeP4([completed(stdout=spec)])
        targeted, shared, unreadable = perforce_cleanup.select_team_workspaces(client, ["ws3"], "alfa")
        self.assertEqual(targeted, [])
        self.assertEqual(shared, [])

    def test_workspace_con_view_vuota_non_compare_in_nessuna_lista(self):
        # View: presente ma senza righe sotto: nessun depot mappato, non è né
        # targeted né shared.
        spec = (
            "Client:\tws4\n"
            "View:\n"
        )
        client = FakeP4([completed(stdout=spec)])
        targeted, shared, unreadable = perforce_cleanup.select_team_workspaces(client, ["ws4"], "alfa")
        self.assertEqual(targeted, [])
        self.assertEqual(shared, [])
        self.assertEqual(unreadable, [])

    def test_workspace_con_altro_depot_tra_virgolette_e_shared(self):
        # Se il depot Beta sfugge al parser, ws5 sembra solo di Alfa e
        # --team Alfa lo cancella mentre lo studente lo usa ancora per Beta.
        spec = (
            "Client:\tws5\n"
            "View:\n"
            "\t//Alfa/... //ws5/Alfa/...\n"
            "\t\"+//Beta/My Assets/...\" \"//ws5/My Assets/...\"\n"
        )
        client = FakeP4([completed(stdout=spec)])
        targeted, shared, unreadable = perforce_cleanup.select_team_workspaces(client, ["ws5"], "alfa")
        self.assertEqual(targeted, [])
        self.assertEqual(shared, ["ws5"])

    def test_workspace_non_leggibile_e_segnalato(self):
        # `client -o` fallito: non si sa cosa mappa, quindi non si tocca, ma
        # l'operatore lo deve sapere.
        client = FakeP4([completed(returncode=1, stderr="timeout")])
        targeted, shared, unreadable = perforce_cleanup.select_team_workspaces(client, ["ws6"], "alfa")
        self.assertEqual(targeted, [])
        self.assertEqual(shared, [])
        self.assertEqual(unreadable, ["ws6"])


# ── purge() non si autoconferma ─────────────────────────────────
class TestPurgeConfirm(unittest.TestCase):
    def test_purge_senza_conferma_esplicita_non_parte(self):
        import naba_store
        with mock.patch.object(naba_store, "_request") as req:
            with self.assertRaises(naba_store.StoreError):
                naba_store.purge("token", "")
        req.assert_not_called()


# ── main() non cancella l'account dopo un errore precedente (2b/2c) ──
class TestCleanupNonCancellaUtenteSeUnErroreCePrecedente(unittest.TestCase):
    def test_delete_user_non_chiamato_se_delete_workspace_e_fallito(self):
        fake = FakeP4()
        delete_user_mock = mock.MagicMock(return_value=(True, ""))
        with mock.patch.object(sys, "argv", ["perforce_cleanup.py", "--user", "mario_rossi"]), \
             mock.patch("builtins.input", return_value="CONFIRM"), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
             mock.patch.object(perforce_cleanup.naba_store, "worker_url", return_value="https://worker.test"), \
             mock.patch.object(perforce_cleanup.naba_store, "get_admin_token", return_value="tok"), \
             mock.patch.object(perforce_cleanup.naba_store, "fetch_users", return_value=[]), \
             mock.patch.object(p4c, "ask_p4_connection", return_value=fake), \
             mock.patch.object(p4c, "connect", return_value=completed()), \
             mock.patch.object(p4c, "user_exists", return_value=True), \
             mock.patch.object(p4c, "user_groups", return_value=[]), \
             mock.patch.object(p4c, "user_workspaces", return_value=["ws1"]), \
             mock.patch.object(p4c, "pending_changes", return_value=[]), \
             mock.patch.object(p4c, "delete_workspace", return_value=(False, "boom")), \
             mock.patch.object(p4c, "delete_user", delete_user_mock):
            perforce_cleanup.main()

        delete_user_mock.assert_not_called()
        # 2b: l'utente trattenuto va detto in chiaro nell'output, non solo
        # inferito dal fatto che delete_user non è stato chiamato.
        self.assertIn(
            "[trattenuto] Utente 'mario_rossi' non cancellato: 1 oggetto/i sopra non rimossi",
            out.getvalue(),
        )


class TestCleanupNonToccaIlKvSePerforceHaFallito(unittest.TestCase):
    ROWS = [{"username": "mario_rossi", "team": "Alfa", "status": "created"}]

    def tearDown(self):
        perforce_cleanup.P4 = None

    def run_cleanup(self, argv, fake=None, **p4_values):
        values = {
            "user_exists": True,
            "user_groups": ["Alfa"],
            "user_workspaces": ["ws1"],
            "pending_changes": [],
            "remove_user_from_group": (True, "", True, 1),
            "delete_workspace": (True, ""),
            "delete_user": (True, ""),
        }
        values.update(p4_values)
        store = perforce_cleanup.naba_store
        patch_status = mock.MagicMock(return_value={"updated": 1, "failed": []})
        delete_record = mock.MagicMock(return_value=1)
        with contextlib.ExitStack() as stack:
            enter = stack.enter_context
            enter(mock.patch.object(sys, "argv", ["perforce_cleanup.py", *argv]))
            enter(mock.patch("builtins.input", return_value="CONFIRM"))
            out = enter(mock.patch("sys.stdout", new_callable=io.StringIO))
            enter(mock.patch.object(store, "worker_url", return_value="https://worker.test"))
            enter(mock.patch.object(store, "get_admin_token", return_value="tok"))
            enter(mock.patch.object(store, "fetch_users", return_value=self.ROWS))
            enter(mock.patch.object(store, "patch_status", patch_status))
            enter(mock.patch.object(store, "delete_user", delete_record))
            enter(mock.patch.object(p4c, "ask_p4_connection", return_value=fake or FakeP4()))
            enter(mock.patch.object(p4c, "connect", return_value=completed()))
            for name, value in values.items():
                enter(mock.patch.object(p4c, name, return_value=value))
            perforce_cleanup.main()
        return out.getvalue(), patch_status, delete_record

    def test_senza_errori_i_record_passano_a_removed(self):
        _, patch_status, _ = self.run_cleanup(["--user", "mario_rossi"])
        patch_status.assert_called_once()

    def test_account_trattenuto_lascia_i_record_come_sono(self):
        # L'account esiste ancora: segnarlo 'removed' farebbe credere il contrario.
        out, patch_status, _ = self.run_cleanup(
            ["--user", "mario_rossi"], delete_workspace=(False, "boom"))
        patch_status.assert_not_called()
        self.assertIn("[saltato] KV non aggiornato: 1 errore/i su Perforce", out)

    def test_con_delete_record_e_un_errore_i_record_restano(self):
        _, _, delete_record = self.run_cleanup(
            ["--user", "mario_rossi", "--delete-record"], delete_workspace=(False, "boom"))
        delete_record.assert_not_called()

    def test_workspace_non_leggibile_e_nominato_e_conta_come_errore(self):
        # --team Alfa con Beta che resta: si guarda la View di ws1, ma
        # `client -o` fallisce.
        fake = FakeP4([completed(returncode=1, stderr="timeout")])
        out, patch_status, _ = self.run_cleanup(
            ["--user", "mario_rossi", "--team", "Alfa"], fake=fake,
            user_groups=["Alfa", "Beta"])
        self.assertIn("Workspace non letti:  ws1", out)
        self.assertIn("COMPLETATO CON 1 ERRORE/I", out)
        patch_status.assert_not_called()

    def test_kv_saltato_non_annuncia_lallineamento_del_kv(self):
        out, _, _ = self.run_cleanup(
            ["--user", "mario_rossi"], delete_workspace=(False, "boom"))
        self.assertNotIn("Il KV può metterci", out)

    def test_dry_run_con_un_errore_non_promette_laggiornamento_del_kv(self):
        # L'anteprima deve dire quello che farà il giro vero: con un workspace
        # non letto il KV viene saltato.
        fake = FakeP4([completed(returncode=1, stderr="timeout")])
        out, _, _ = self.run_cleanup(
            ["--user", "mario_rossi", "--team", "Alfa", "--dry-run"], fake=fake,
            user_groups=["Alfa", "Beta"])
        self.assertNotIn("porterebbe a 'removed'", out)
        self.assertIn("[dry-run] KV non verrebbe aggiornato: 1 errore/i su Perforce", out)


class TestPruneNonCancellaLutenteOrfanoSeUnErroreCePrecedente(unittest.TestCase):
    def test_delete_user_non_chiamato_se_delete_workspace_e_fallito(self):
        fake = FakeP4()
        delete_user_mock = mock.MagicMock(return_value=(True, ""))
        with mock.patch.object(sys, "argv", ["perforce_prune.py"]), \
             mock.patch("builtins.input", return_value="CONFIRM"), \
             mock.patch("sys.stdout", new_callable=io.StringIO), \
             mock.patch.object(p4c, "ask_p4_connection", return_value=fake), \
             mock.patch.object(p4c, "connect", return_value=completed()), \
             mock.patch.object(perforce_prune, "get_all_users", return_value=["orfano"]), \
             mock.patch.object(perforce_prune, "get_users_in_groups", return_value=set()), \
             mock.patch.object(perforce_prune, "get_users_in_protections", return_value=set()), \
             mock.patch.object(perforce_prune, "get_groups_in_protections", return_value=set()), \
             mock.patch.object(perforce_prune, "get_all_groups", return_value=[]), \
             mock.patch.object(p4c, "pending_changes", return_value=[]), \
             mock.patch.object(p4c, "user_workspaces", return_value=["ws1"]), \
             mock.patch.object(p4c, "delete_workspace", return_value=(False, "boom")), \
             mock.patch.object(p4c, "delete_user", delete_user_mock):
            perforce_prune.main()

        delete_user_mock.assert_not_called()

    def test_orfano_pulito_viene_cancellato_anche_se_un_altro_orfano_fallisce(self):
        # Due orfani: "fallito" (il suo workspace non si cancella) e "pulito"
        # (va liscio). prune deve trattenere solo il primo e completare il
        # secondo — un errore su un utente non deve bloccare gli altri.
        fake = FakeP4()
        delete_user_mock = mock.MagicMock(return_value=(True, ""))
        delete_workspace_mock = mock.MagicMock(side_effect=[(False, "boom"), (True, "")])
        with mock.patch.object(sys, "argv", ["perforce_prune.py"]), \
             mock.patch("builtins.input", return_value="CONFIRM"), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
             mock.patch.object(p4c, "ask_p4_connection", return_value=fake), \
             mock.patch.object(p4c, "connect", return_value=completed()), \
             mock.patch.object(perforce_prune, "get_all_users", return_value=["fallito", "pulito"]), \
             mock.patch.object(perforce_prune, "get_users_in_groups", return_value=set()), \
             mock.patch.object(perforce_prune, "get_users_in_protections", return_value=set()), \
             mock.patch.object(perforce_prune, "get_groups_in_protections", return_value=set()), \
             mock.patch.object(perforce_prune, "get_all_groups", return_value=[]), \
             mock.patch.object(p4c, "pending_changes", return_value=[]), \
             mock.patch.object(p4c, "user_workspaces", return_value=["ws1"]), \
             mock.patch.object(p4c, "delete_workspace", delete_workspace_mock), \
             mock.patch.object(p4c, "delete_user", delete_user_mock):
            perforce_prune.main()

        delete_user_mock.assert_called_once_with(fake, "pulito", False)
        self.assertIn(
            "[kept] User 'fallito' kept: 1 object(s) above could not be removed",
            out.getvalue(),
        )


# ── get_all_users() esclude account admin/servizio (contratto 3c) ──
class TestExportPUsersEscludeAccount(unittest.TestCase):
    def tearDown(self):
        export_p4_users.P4 = None

    def test_utente_escluso_per_nome_esatto_non_viene_letto_ne_incluso(self):
        export_p4_users.P4 = FakeP4([
            completed(stdout=(
                "villal <v@x> (V) accessed 2026/01/01\n"
                "mario_rossi <m@x> (M) accessed 2026/01/01\n"
            )),
            completed(stdout="FullName:\tMario Rossi\nEmail:\tm@x\n"),
        ])
        with mock.patch.object(export_p4_users, "EXCLUDE_USERS", {"villal"}):
            users = export_p4_users.get_all_users()

        self.assertEqual([u["username"] for u in users], ["mario_rossi"])
        self.assertNotIn(("user", "-o", "villal"),
                         [c[0] for c in export_p4_users.P4.calls])

    def test_esclusione_ignora_maiuscole_e_minuscole(self):
        # EXCLUDE_USERS contiene nomi lowercase (come impone il contratto);
        # l'utente arriva da "p4 users" con la capitalizzazione originale.
        export_p4_users.P4 = FakeP4([
            completed(stdout="VillaL <v@x> (V) accessed 2026/01/01\n"),
        ])
        with mock.patch.object(export_p4_users, "EXCLUDE_USERS", {"villal"}):
            users = export_p4_users.get_all_users()

        self.assertEqual(users, [])
        self.assertNotIn(("user", "-o", "VillaL"),
                         [c[0] for c in export_p4_users.P4.calls])


# ── get_user_groups() esclude account admin/servizio (contratto 3c) ──
class TestExportPUserGroupsEscludeAccount(unittest.TestCase):
    def tearDown(self):
        export_p4_users.P4 = None

    def test_membro_escluso_case_insensitive_non_compare_ma_gli_altri_si(self):
        group_spec = (
            "Group:\tAlfa\n"
            "Users:\n"
            "\tVillaL\n"
            "\tmario_rossi\n"
        )
        export_p4_users.P4 = FakeP4([
            completed(stdout="Alfa\n"),
            completed(stdout=group_spec),
        ])
        with mock.patch.object(export_p4_users, "EXCLUDE_USERS", {"villal"}):
            groups = export_p4_users.get_user_groups()

        self.assertNotIn("VillaL", groups)
        self.assertNotIn("villal", groups)
        self.assertEqual(groups, {"mario_rossi": ["Alfa"]})


# ── Team: gruppo, depot e protezione ────────────────────────────
PROTECT_SPEC = (
    "Protections:\n"
    "\tsuper user admin * //...\n"
)


class FakeServer(FakeP4):
    """Server finto con stato: gruppi, depot e tabella delle protezioni."""

    def __init__(self, groups=(), depots=(), protections=PROTECT_SPEC, protect_o_fails=False,
                 fail=()):
        super().__init__()
        self.groups = list(groups)
        self.depots = list(depots)
        self.protections = protections
        self.protect_o_fails = protect_o_fails
        self.fail = set(fail)  # comandi che falliscono, es. {"groups"}

    def run(self, *args, stdin_text=None):
        self.calls.append((args, stdin_text))
        if args[0] in self.fail:
            return completed(returncode=1, stderr="timeout")
        if args == ("groups",):
            return completed(stdout="".join(f"{g}\n" for g in self.groups))
        if args == ("depots",):
            return completed(stdout="".join(f"Depot {d} 2020/01/01 local {d}/... ''\n" for d in self.depots))
        if args == ("protect", "-o"):
            if self.protect_o_fails:
                return completed(returncode=1, stderr="timeout")
            return completed(stdout=self.protections)
        if args == ("protect", "-i"):
            self.protections = stdin_text
        elif args == ("group", "-i"):
            self.groups.append(stdin_text.split("\n")[0].split("\t")[1])
        elif args == ("depot", "-i"):
            self.depots.append(stdin_text.split("\n")[0].split("\t")[1])
        return completed()

    def writes(self):
        return [c[0] for c in self.calls if c[0][-1] == "-i"]


def team_line(team):
    return f"\twrite group {team} * //{team}/...\n"


class TestSetupTeam(unittest.TestCase):
    def tearDown(self):
        perforce_provision.P4 = None

    def setup(self, server, team):
        perforce_provision.P4 = server
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            ok = perforce_provision.setup_team(team)
        return ok, out.getvalue()

    def test_team_nuovo_scrive_prima_la_protezione_poi_gruppo_e_depot(self):
        # La protezione per prima: è il segno che il team è di questo flusso,
        # e un giro interrotto a metà si riprende rilanciando.
        server = FakeServer()
        ok, _ = self.setup(server, "Alfa")
        self.assertTrue(ok)
        self.assertEqual(server.writes(), [("protect", "-i"), ("group", "-i"), ("depot", "-i")])
        self.assertIn(team_line("Alfa"), server.protections)

    def test_depot_esistente_non_nostro_viene_rifiutato(self):
        # Il team "depot" non deve ottenere la scrittura sul depot di default.
        server = FakeServer(depots=["depot"])
        ok, out = self.setup(server, "depot")
        self.assertFalse(ok)
        self.assertEqual(server.writes(), [])
        self.assertIn("non creato da questo flusso", out)

    def test_gruppo_esistente_non_nostro_viene_rifiutato(self):
        # Un team che si chiama come un gruppo admin non deve entrarci.
        server = FakeServer(groups=["p4admins"])
        ok, _ = self.setup(server, "p4admins")
        self.assertFalse(ok)
        self.assertEqual(server.writes(), [])

    def test_team_con_protezione_si_riprende_dopo_un_giro_interrotto(self):
        # Giro precedente: protezione e depot scritti, gruppo no (o sparito
        # con l'ultimo membro). Il rilancio completa senza rifiutare.
        server = FakeServer(depots=["Alfa"], protections=PROTECT_SPEC + team_line("Alfa"))
        ok, _ = self.setup(server, "Alfa")
        self.assertTrue(ok)
        self.assertEqual(server.writes(), [("group", "-i")])

    def test_riga_di_un_altra_grafia_non_rende_nostro_il_gruppo(self):
        # La riga di "Alfa" non basta per il gruppo "alfa": nel dubbio si
        # rifiuta e decide l'operatore. Il caso normale (studente che scrive
        # "alfa" per il team "Alfa") passa da canonical_team, che usa "Alfa".
        server = FakeServer(groups=["alfa"], depots=["Alfa"],
                            protections=PROTECT_SPEC + team_line("Alfa"))
        ok, _ = self.setup(server, "alfa")
        self.assertFalse(ok)
        self.assertEqual(server.writes(), [])

    def test_protezioni_non_leggibili_nessuna_scrittura(self):
        server = FakeServer(protect_o_fails=True)
        ok, _ = self.setup(server, "Alfa")
        self.assertFalse(ok)
        self.assertEqual(server.writes(), [])

    def test_elenco_gruppi_non_leggibile_rifiuta_il_team(self):
        # Se `p4 groups` fallisce non si sa se "p4admins" esiste: nel dubbio
        # il team si rifiuta, non si accetta.
        server = FakeServer(groups=["p4admins"], fail={"groups"})
        ok, _ = self.setup(server, "p4admins")
        self.assertFalse(ok)
        self.assertEqual(server.writes(), [])

    def test_elenco_depot_non_leggibile_rifiuta_il_team(self):
        server = FakeServer(depots=["depot"], fail={"depots"})
        ok, _ = self.setup(server, "depot")
        self.assertFalse(ok)
        self.assertEqual(server.writes(), [])

    def test_team_con_protezione_ma_elenco_depot_non_leggibile_non_riesce(self):
        # Giro ripreso: la riga c'è, il depot no. Se `p4 depots` fallisce, il
        # depot non va dato per esistente: il team non è pronto.
        server = FakeServer(protections=PROTECT_SPEC + team_line("Alfa"), fail={"depots"})
        ok, _ = self.setup(server, "Alfa")
        self.assertFalse(ok)

    def test_create_group_con_elenco_non_leggibile_non_riesce(self):
        perforce_provision.P4 = FakeServer(fail={"groups"})
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertFalse(perforce_provision.create_group("Alfa"))
        self.assertEqual(perforce_provision.P4.writes(), [])

    def test_create_depot_con_elenco_non_leggibile_non_riesce(self):
        perforce_provision.P4 = FakeServer(fail={"depots"})
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertFalse(perforce_provision.create_depot("Alfa"))
        self.assertEqual(perforce_provision.P4.writes(), [])

    def test_le_protezioni_si_leggono_una_volta_sola(self):
        server = FakeServer()
        self.setup(server, "Alfa")
        self.assertEqual([c[0] for c in server.calls].count(("protect", "-o")), 1)

    def test_riga_di_esclusione_non_vale_come_permesso(self):
        # "-write group Alfa ..." toglie il permesso: add_protection non deve
        # scambiarla per la riga del team e saltare la scrittura.
        server = FakeServer(protections=PROTECT_SPEC + "\t-write group Alfa * //Alfa/...\n")
        ok, _ = self.setup(server, "Alfa")
        self.assertTrue(ok)
        self.assertIn(("protect", "-i"), server.writes())
        self.assertTrue(perforce_provision.has_team_protection(server.protections, "Alfa"))

    def test_riga_di_permesso_confrontata_con_le_maiuscole(self):
        # Su un server case-sensitive la riga di "staff" non dice niente di "Staff".
        spec = PROTECT_SPEC + team_line("staff")
        self.assertTrue(perforce_provision.has_team_protection(spec, "staff"))
        self.assertFalse(perforce_provision.has_team_protection(spec, "Staff"))

    def test_create_depot_riconosce_il_depot_con_altre_maiuscole(self):
        # Su un server case-insensitive "DEPOT" è il depot di default.
        perforce_provision.P4 = FakeServer(depots=["depot"])
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertTrue(perforce_provision.create_depot("DEPOT"))
        self.assertEqual(perforce_provision.P4.writes(), [])

    def test_create_group_riconosce_il_gruppo_con_altre_maiuscole(self):
        # Su un server case-insensitive "group -i" con Users: vuoto
        # sovrascriverebbe ProjectAlpha e ne toglierebbe tutti i membri.
        perforce_provision.P4 = FakeServer(groups=["ProjectAlpha"])
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertTrue(perforce_provision.create_group("projectalpha"))
        self.assertEqual(perforce_provision.P4.writes(), [])


class TestCanonicalTeam(unittest.TestCase):
    def tearDown(self):
        perforce_provision.P4 = None

    def test_usa_la_grafia_del_gruppo_esistente(self):
        perforce_provision.P4 = FakeServer(groups=["ProjectAlpha"])
        self.assertEqual(perforce_provision.canonical_team("projectalpha"), "ProjectAlpha")

    def test_usa_la_grafia_del_depot_esistente(self):
        perforce_provision.P4 = FakeServer(depots=["depot"])
        self.assertEqual(perforce_provision.canonical_team("DEPOT"), "depot")

    def test_senza_corrispondenze_lascia_il_nome(self):
        perforce_provision.P4 = FakeServer(groups=["Beta"])
        self.assertEqual(perforce_provision.canonical_team("Gamma"), "Gamma")

    def test_preferisce_la_grafia_esatta(self):
        # Server case-sensitive con "Staff" (admin) e "staff" (un team):
        # chi scrive "staff" resta in "staff".
        perforce_provision.P4 = FakeServer(groups=["Staff", "staff"])
        self.assertEqual(perforce_provision.canonical_team("staff"), "staff")


def pending_row(username, team):
    return {"username": username, "full_name": username.replace("_", " ").title(),
            "email": f"{username}@x.it", "team": team, "status": "pending"}


class TestProvisionMain(unittest.TestCase):
    def tearDown(self):
        perforce_provision.P4 = None

    def run_provision(self, server, rows, argv=()):
        store = perforce_provision.naba_store
        patch_status = mock.MagicMock(return_value={"updated": len(rows), "failed": []})
        self.create_user = mock.MagicMock(return_value=True)
        self.add_user_to_group = mock.MagicMock(return_value=True)
        with contextlib.ExitStack() as stack:
            enter = stack.enter_context
            enter(mock.patch.object(sys, "argv", ["perforce_provision.py", "--skip-discord",
                                                  "--skip-email", *argv]))
            enter(mock.patch("sys.stdout", new_callable=io.StringIO))
            enter(mock.patch.object(store, "worker_url", return_value="https://worker.test"))
            enter(mock.patch.object(store, "get_admin_token", return_value="tok"))
            enter(mock.patch.object(store, "fetch_users", return_value=rows))
            enter(mock.patch.object(store, "patch_status", patch_status))
            enter(mock.patch.object(p4c, "ask_p4_connection", return_value=server))
            enter(mock.patch.object(p4c, "connect", return_value=completed()))
            enter(mock.patch.object(perforce_provision, "ask_initial_password", return_value=None))
            enter(mock.patch.object(perforce_provision, "create_user", self.create_user))
            enter(mock.patch.object(perforce_provision, "add_user_to_group", self.add_user_to_group))
            perforce_provision.main()
        if not patch_status.called:
            return []
        return [u["status"] for u in patch_status.call_args[0][1]]

    def test_team_rifiutato_nessun_account_e_tutti_in_errore(self):
        # Niente account per chi finisce in un team rifiutato: sarebbero
        # licenze occupate da utenti senza gruppo.
        server = FakeServer(depots=["depot"])
        rows = [pending_row("mario_rossi", "depot"), pending_row("anna_bianchi", "depot")]
        statuses = self.run_provision(server, rows)
        self.assertEqual(statuses, ["error", "error"])
        self.create_user.assert_not_called()
        self.add_user_to_group.assert_not_called()

    def test_team_nuovo_va_a_buon_fine(self):
        server = FakeServer()
        rows = [pending_row("mario_rossi", "Alfa"), pending_row("anna_bianchi", "Alfa")]
        statuses = self.run_provision(server, rows)
        self.assertEqual(statuses, ["created", "created"])
        self.assertEqual(self.create_user.call_count, 2)
        self.assertEqual([c.args[1] for c in self.add_user_to_group.call_args_list], ["Alfa", "Alfa"])

    def test_varianti_di_maiuscole_diventano_un_solo_team(self):
        # Anche nel primo giro, quando il gruppo non esiste ancora: un depot,
        # una protezione, e lo stesso nome per Discord ed email.
        server = FakeServer()
        rows = [pending_row("mario_rossi", "ProjectAlpha"), pending_row("anna_bianchi", "projectalpha")]
        statuses = self.run_provision(server, rows)
        self.assertEqual(statuses, ["created", "created"])
        self.assertEqual(server.depots, ["ProjectAlpha"])
        self.assertEqual(server.protections.count("write group"), 1)
        self.assertEqual([c.args[1] for c in self.add_user_to_group.call_args_list],
                         ["ProjectAlpha", "ProjectAlpha"])
        self.assertEqual([r["team"] for r in rows], ["ProjectAlpha", "ProjectAlpha"])

    def test_variante_di_maiuscole_di_un_team_esistente_usa_il_suo_nome(self):
        server = FakeServer(groups=["ProjectAlpha"], depots=["ProjectAlpha"],
                            protections=PROTECT_SPEC + team_line("ProjectAlpha"))
        rows = [pending_row("anna_bianchi", "projectalpha")]
        statuses = self.run_provision(server, rows)
        self.assertEqual(statuses, ["created"])
        self.assertEqual(server.writes(), [])
        self.assertEqual(self.add_user_to_group.call_args.args[1], "ProjectAlpha")

    def test_variante_di_maiuscole_non_entra_in_un_gruppo_admin(self):
        # Server case-sensitive: "Staff" è un gruppo admin, "staff" un team.
        # "STAFF" non ha grafia esatta: la prima corrispondenza è "Staff", che
        # non ha la riga del team, quindi il team si rifiuta.
        server = FakeServer(groups=["Staff", "staff"], depots=["staff"],
                            protections=PROTECT_SPEC + team_line("staff"))
        statuses = self.run_provision(server, [pending_row("mario_rossi", "STAFF")])
        self.assertEqual(statuses, ["error"])
        self.add_user_to_group.assert_not_called()

    def test_team_che_differiscono_solo_per_maiuscole_restano_separati(self):
        # Server case-sensitive: "Staff" è un gruppo admin, "staff" un team.
        # Il rifiuto della riga "Staff" non deve bloccare i membri di "staff",
        # qualunque sia l'ordine delle righe.
        server = FakeServer(groups=["Staff", "staff"], depots=["staff"],
                            protections=PROTECT_SPEC + team_line("staff"))
        rows = [pending_row("intruso", "Staff"), pending_row("mario_rossi", "staff")]
        statuses = self.run_provision(server, rows)
        self.assertEqual(statuses, ["error", "created"])
        self.assertEqual([c.args[1] for c in self.add_user_to_group.call_args_list], ["staff"])

    def test_record_in_errore_ripassano_solo_con_retry_errors(self):
        # Un team rifiutato manda i record in 'error': dopo aver sistemato, il
        # rilancio deve poterli riprendere.
        rows = [dict(pending_row("mario_rossi", "Alfa"), status="error")]
        self.run_provision(FakeServer(), [dict(r) for r in rows])
        self.create_user.assert_not_called()
        statuses = self.run_provision(FakeServer(), [dict(r) for r in rows], argv=["--retry-errors"])
        self.assertEqual(statuses, ["created"])

    def test_dry_run_con_varianti_di_maiuscole_mostra_un_solo_team(self):
        server = FakeServer()
        rows = [pending_row("mario_rossi", "ProjectAlpha"), pending_row("anna_bianchi", "projectalpha")]
        self.run_provision(server, rows, argv=["--dry-run"])
        self.assertEqual(server.writes(), [])
        self.assertEqual([c.args[1] for c in self.add_user_to_group.call_args_list],
                         ["ProjectAlpha", "ProjectAlpha"])


# ── ask_initial_password() con conferma (contratto 3d) ──────────
class TestAskInitialPassword(unittest.TestCase):
    def test_invio_vuoto_non_imposta_la_password_e_non_chiede_conferma(self):
        with mock.patch("perforce_provision.getpass.getpass", side_effect=[""]) as gp:
            self.assertIsNone(perforce_provision.ask_initial_password())
        self.assertEqual(gp.call_count, 1)

    def test_due_password_uguali_vengono_accettate(self):
        with mock.patch("perforce_provision.getpass.getpass", side_effect=["segreta", "segreta"]):
            self.assertEqual(perforce_provision.ask_initial_password(), "segreta")

    def test_due_password_diverse_fermano_lo_script(self):
        with mock.patch("perforce_provision.getpass.getpass", side_effect=["segreta", "sbagliata"]):
            with self.assertRaises(SystemExit) as ctx:
                perforce_provision.ask_initial_password()
        self.assertEqual(ctx.exception.code, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
