// Test del form di index.html: lo <script> gira in una sandbox con un DOM
// finto, e si guida il submit con risposte del worker preparate.
// Run with: npm test
import fs from "node:fs";
import vm from "node:vm";

const html = fs.readFileSync(new URL("../index.html", import.meta.url), "utf8");
const code = html.slice(html.indexOf("<script>") + "<script>".length, html.lastIndexOf("</script>"));

// ── DOM finto ─────────────────────────────────────────────────
function fakeEl(extra = {}) {
  const classes = new Set();
  return {
    value: "", checked: false, textContent: "", className: "", disabled: false,
    innerHTML: "", style: {}, previousElementSibling: null, resetCalls: 0,
    handlers: {},
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      toggle: (c, on) => ((on ?? !classes.has(c)) ? classes.add(c) : classes.delete(c)),
      contains: (c) => classes.has(c),
    },
    addEventListener(type, fn) { this.handlers[type] = fn; },
    setAttribute() {}, getAttribute: () => null, appendChild() {},
    querySelector: () => fakeEl(), querySelectorAll: () => [],
    reset() { this.resetCalls++; },
    ...extra,
  };
}

/** Carica il form. `members`: [nome, cognome, email] dei membri del gruppo. */
function loadForm({ status = 200, body, members = [] }) {
  const byId = {};
  const groupButton = fakeEl({ getAttribute: () => "group" });
  const cards = members.map(([n, c, e]) => fakeEl({
    querySelectorAll: () => [fakeEl({ value: n }), fakeEl({ value: c }), fakeEl({ value: e })],
  }));
  const document = {
    documentElement: {},
    getElementById: (id) => (byId[id] ??= fakeEl()),
    querySelectorAll: (sel) =>
      sel === ".member-card" ? cards :
      sel === "#thesis-segmented .seg-btn" ? [groupButton] : [],
    createElement: () => fakeEl(),
  };
  const sent = [];
  const fetch = async (url, opts) => {
    sent.push(JSON.parse(opts.body));
    return { ok: status < 400, status, json: async () => body };
  };
  vm.runInNewContext(code, { document, window: {}, fetch, console, setTimeout });
  return { byId, groupButton, sent };
}

async function submit(form, { nome, cognome, team = "Alfa", group = false }) {
  const { byId } = form;
  byId.nome.value = nome;
  byId.cognome.value = cognome;
  byId.email.value = "studente@studenti.naba.it";
  byId.team.value = team;
  if (group) {
    byId.tesista.checked = true;
    form.groupButton.handlers.click();
  } else {
    byId.anno_corso.value = "1";
  }
  await byId.regForm.handlers.submit({ preventDefault() {} });
}

// ── Runner ────────────────────────────────────────────────────
let pass = 0, fail = 0;
const failures = [];

function check(label, cond, detail = "") {
  if (cond) { pass++; console.log(`  ok   ${label}`); }
  else { fail++; failures.push(label); console.log(`  FAIL ${label}${detail ? " — " + detail : ""}`); }
}

const allStored = { success: true, stored: 1, results: [{ username: "x", ok: true, status: "pending" }] };

async function main() {
  // ── 1. Username inviato ──
  console.log("\n1. Username inviato");
  {
    const form = loadForm({ body: allStored });
    await submit(form, { nome: "李", cognome: "Wang" });
    const u = form.sent[0].users[0].username;
    check("nome senza lettere latine → come l'anteprima, senza '_' iniziale", u === "wang", u);
  }
  {
    const form = loadForm({ body: allStored });
    await submit(form, { nome: "Mario", cognome: "Rossi" });
    const u = form.sent[0].users[0].username;
    check("nome e cognome latini → nome_cognome", u === "mario_rossi", u);
    check("tutti registrati → successo e form svuotato",
      form.byId.statusMsg.className === "status-message success" && form.byId.regForm.resetCalls === 1,
      form.byId.statusMsg.className);
  }

  // ── 2. Membri rifiutati ──
  console.log("\n2. Membri rifiutati");
  {
    const body = {
      success: true, stored: 1,
      results: [
        { username: "mario_rossi", ok: true, status: "pending" },
        { username: "anna_ivanova", ok: false, error: "email non valida" },
      ],
    };
    const form = loadForm({ body, members: [["Anna", "Ivanova", "anna@studenti.naba.it"]] });
    await submit(form, { nome: "Mario", cognome: "Rossi", group: true });
    const text = form.byId.statusText.textContent;
    check("un membro rifiutato → errore, non successo",
      form.byId.statusMsg.className === "status-message error", form.byId.statusMsg.className);
    check("il messaggio nomina il membro e il motivo",
      text.includes("Anna Ivanova (email non valida)"), text);
    check("il form resta compilato", form.byId.regForm.resetCalls === 0);
    check("il bottone torna attivo", form.byId.submitBtn.disabled === false);
  }
  {
    // Reinvio: il primo è già registrato, il membro è di nuovo rifiutato.
    // Il worker risponde 400 perché non ha salvato niente.
    const body = {
      success: false, error: "email non valida",
      results: [
        { username: "mario_rossi", ok: true, status: "already_exists" },
        { username: "anna_ivanova", ok: false, error: "email non valida" },
      ],
    };
    const form = loadForm({ status: 400, body, members: [["Anna", "Ivanova", "anna@studenti.naba.it"]] });
    await submit(form, { nome: "Mario", cognome: "Rossi", group: true });
    const text = form.byId.statusText.textContent;
    check("reinvio con 400 → nomina ancora il membro rifiutato",
      text.includes("Anna Ivanova (email non valida)"), text);
  }
  {
    const body = {
      success: true, stored: 1,
      results: [
        { username: "mario_rossi", ok: true, status: "pending" },
        { username: "", ok: false, error: "username non valido" },
      ],
    };
    const form = loadForm({ body, members: [["Anna $'", "Иванова", "anna@studenti.naba.it"]] });
    await submit(form, { nome: "Mario", cognome: "Rossi", group: true });
    const text = form.byId.statusText.textContent;
    check("un nome con $' compare com'è", text.includes("Anna $' Иванова"), text);
  }

  {
    // Un nome senza lettere latine dà uno username vuoto: il form lo ferma
    // prima di inviare, invece di registrare solo metà del gruppo.
    const form = loadForm({ body: allStored, members: [["Анна", "Иванова", "anna@studenti.naba.it"]] });
    await submit(form, { nome: "Mario", cognome: "Rossi", group: true });
    const text = form.byId.statusText.textContent;
    check("membro con nome non latino → fermato nel form, niente invio", form.sent.length === 0,
      `inviati ${form.sent.length}`);
    check("il messaggio nomina il membro e chiede i caratteri latini",
      text.includes("Анна Иванова") && text.includes("caratteri latini"), text);
  }

  {
    // Il worker tronca lo username a 64 caratteri prima di validarlo: un
    // nome lungo non va fermato dal form.
    const form = loadForm({ body: allStored });
    await submit(form, {
      nome: "Maria Guadalupe Fernanda Josefina Antonia",
      cognome: "de los Santos Rodriguez y Garcia Lopez",
    });
    check("nome oltre 64 caratteri → inviato come fa il worker", form.sent.length === 1,
      form.byId.statusText.textContent);
  }

  // ── 3. Nome del team ──
  console.log("\n3. Nome del team");
  for (const team of ["2024", "a...b", "Project Alpha"]) {
    const form = loadForm({ body: allStored });
    await submit(form, { nome: "Mario", cognome: "Rossi", team });
    check(`team "${team}" → fermato nel form, niente invio`,
      form.sent.length === 0 && form.byId["team-error"].classList.contains("visible"),
      `inviati ${form.sent.length}`);
  }
  {
    const form = loadForm({ body: allStored });
    await submit(form, { nome: "Mario", cognome: "Rossi", team: "Team2024.v2" });
    check("team con cifre e punti singoli → inviato", form.sent.length === 1);
  }

  console.log(`\n${"=".repeat(52)}`);
  console.log(`RISULTATO: ${pass} ok, ${fail} falliti`);
  if (fail) {
    console.log("\nFalliti:");
    failures.forEach((f) => console.log("  - " + f));
    process.exit(1);
  }
}

main().catch((e) => { console.error("ERRORE FATALE:", e); process.exit(1); });
