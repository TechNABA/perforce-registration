// Functional test suite for worker.js, backed by an in-memory fake KV.
// Run with: npm test
import worker from "../worker.js";

// ── Mock KV ───────────────────────────────────────────────────
class MockKV {
  constructor() { this.store = new Map(); }

  async get(key, options) {
    const entry = this.store.get(key);
    if (!entry) return null;
    if (options && options.type === "json") return JSON.parse(entry.value);
    return entry.value;
  }

  async put(key, value, options = {}) {
    this.store.set(key, { value, metadata: options.metadata ?? null });
  }

  async delete(key) { this.store.delete(key); }

  async list({ prefix = "", cursor, limit = 1000 } = {}) {
    const all = [...this.store.entries()]
      .filter(([name]) => name.startsWith(prefix))
      .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
    const start = cursor ? Number(cursor) : 0;
    const slice = all.slice(start, start + limit);
    const end = start + slice.length;
    return {
      keys: slice.map(([name, e]) => ({ name, metadata: e.metadata })),
      list_complete: end >= all.length,
      cursor: end >= all.length ? undefined : String(end),
    };
  }
}

const ctx = { waitUntil: (p) => { if (p && p.catch) p.catch(() => {}); } };

function makeEnv() {
  return { USERS: new MockKV(), ADMIN_TOKEN: "tok-segreto-123" };
}

const BASE = "https://worker.test";
const ADMIN = { "X-Admin-Token": "tok-segreto-123" };

function req(method, path, { headers = {}, body, origin } = {}) {
  const h = { ...headers };
  if (origin) h["Origin"] = origin;
  if (body !== undefined) h["Content-Type"] = "application/json";
  return new Request(BASE + path, {
    method,
    headers: h,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
}

// ── Runner ────────────────────────────────────────────────────
let pass = 0, fail = 0;
const failures = [];

function check(label, cond, detail = "") {
  if (cond) { pass++; console.log(`  ok   ${label}`); }
  else { fail++; failures.push(label); console.log(`  FAIL ${label}${detail ? " — " + detail : ""}`); }
}

const user = (over = {}) => ({
  timestamp: "2026-05-01T10:00:00.000Z",
  username: "mario_rossi",
  full_name: "Mario Rossi",
  email: "mario.rossi@studenti.naba.it",
  team: "Alpha",
  tesista: "yes",
  anno_corso: "",
  status: "pending",
  ...over,
});

async function main() {
  // ── 1. Endpoint pubblici ──
  console.log("\n1. Endpoint pubblici");
  {
    const env = makeEnv();
    let r = await worker.fetch(req("GET", "/"), env, ctx);
    let b = await r.json();
    check("GET / health", r.status === 200 && b.ok === true);

    r = await worker.fetch(req("GET", "/sconosciuto"), env, ctx);
    check("path sconosciuto → 404", r.status === 404);

    r = await worker.fetch(req("GET", "/"), { ADMIN_TOKEN: "x" }, ctx);
    check("binding USERS mancante → 500", r.status === 500);
  }

  // ── 2. CORS ──
  console.log("\n2. CORS");
  {
    const env = makeEnv();
    let r = await worker.fetch(req("OPTIONS", "/", { origin: "https://p4setup.naba.it" }), env, ctx);
    check("preflight da origin consentita → 204", r.status === 204);
    check("preflight espone Allow-Origin",
      r.headers.get("Access-Control-Allow-Origin") === "https://p4setup.naba.it");

    r = await worker.fetch(req("OPTIONS", "/", { origin: "https://evil.example" }), env, ctx);
    check("preflight da origin estranea → 403", r.status === 403);

    r = await worker.fetch(req("POST", "/", { origin: "https://p4setup.naba.it", body: { users: [user()] } }), env, ctx);
    check("POST rispecchia Allow-Origin",
      r.headers.get("Access-Control-Allow-Origin") === "https://p4setup.naba.it");
  }

  // ── 3. Registrazione dal form ──
  console.log("\n3. POST / registrazione");
  {
    const env = makeEnv();
    let r = await worker.fetch(req("POST", "/", { body: { users: [user()] } }), env, ctx);
    let b = await r.json();
    check("registrazione valida", r.status === 200 && b.success && b.stored === 1, JSON.stringify(b));
    check("status iniziale pending", b.results[0].status === "pending");

    // stesso utente stesso team
    r = await worker.fetch(req("POST", "/", { body: { users: [user()] } }), env, ctx);
    b = await r.json();
    check("stesso utente+team → already_exists", b.results[0].status === "already_exists", JSON.stringify(b));
    check("non crea un secondo record", env.USERS.store.size === 1);

    // stesso username, team diverso
    r = await worker.fetch(req("POST", "/", { body: { users: [user({ team: "Beta" })] } }), env, ctx);
    b = await r.json();
    check("stesso username, team diverso → duplicate", b.results[0].status === "duplicate", JSON.stringify(b));
    check("crea comunque il record", env.USERS.store.size === 2);

    // record non valido
    r = await worker.fetch(req("POST", "/", { body: { users: [user({ email: "non-una-email" })] } }), env, ctx);
    b = await r.json();
    check("email non valida → 400", r.status === 400 && !b.success, JSON.stringify(b));

    r = await worker.fetch(req("POST", "/", { body: { users: [user({ username: "a b c" })] } }), env, ctx);
    check("username non valido → 400", r.status === 400);

    r = await worker.fetch(req("POST", "/", { body: { users: [] } }), env, ctx);
    check("array vuoto → 400", r.status === 400);

    const troppi = Array.from({ length: 26 }, (_, i) => user({ username: `u_${i}` }));
    r = await worker.fetch(req("POST", "/", { body: { users: troppi } }), env, ctx);
    check("batch oltre il limite → 400", r.status === 400);

    r = await worker.fetch(new Request(BASE + "/", { method: "POST", body: "{non json" }), env, ctx);
    check("body non JSON → 400", r.status === 400);
  }

  // ── 4. Autenticazione admin ──
  console.log("\n4. Autenticazione admin");
  {
    const env = makeEnv();
    let r = await worker.fetch(req("GET", "/export"), env, ctx);
    check("senza token → 401", r.status === 401);

    r = await worker.fetch(req("GET", "/export", { headers: { "X-Admin-Token": "sbagliato" } }), env, ctx);
    check("token errato → 401", r.status === 401);

    r = await worker.fetch(req("GET", "/export", { headers: { "X-Admin-Token": "tok-segreto-1234" } }), env, ctx);
    check("token di lunghezza diversa → 401", r.status === 401);

    r = await worker.fetch(req("GET", "/export", { headers: ADMIN }), env, ctx);
    check("token corretto → 200", r.status === 200);

    const noToken = { USERS: new MockKV() };
    r = await worker.fetch(req("GET", "/export", { headers: ADMIN }), noToken, ctx);
    check("ADMIN_TOKEN non configurato → 401", r.status === 401);
  }

  // ── 5. Import ──
  console.log("\n5. POST /import");
  {
    const env = makeEnv();
    let r = await worker.fetch(req("POST", "/import", {
      headers: ADMIN,
      body: { users: [user({ status: "existing" }), user({ username: "lucia_bianchi", full_name: "Lucia Bianchi", email: "lucia.bianchi@naba.it", team: "Beta", status: "existing" })] },
    }), env, ctx);
    let b = await r.json();
    check("import di 2 record", b.success && b.written === 2, JSON.stringify(b));

    r = await worker.fetch(req("GET", "/export", { headers: ADMIN }), env, ctx);
    b = await r.json();
    check("status 'existing' preservato", b.users.every((u) => u.status === "existing"), JSON.stringify(b.users));

    // sovrascrittura
    r = await worker.fetch(req("POST", "/import", { headers: ADMIN, body: { users: [user({ status: "created" })] } }), env, ctx);
    b = await r.json();
    check("import sovrascrive di default", b.written === 1);
    r = await worker.fetch(req("GET", "/export", { headers: ADMIN }), env, ctx);
    b = await r.json();
    check("record aggiornato", b.users.find((u) => u.username === "mario_rossi").status === "created");

    // skip-existing
    r = await worker.fetch(req("POST", "/import?mode=skip-existing", { headers: ADMIN, body: { users: [user({ status: "pending" })] } }), env, ctx);
    b = await r.json();
    check("mode=skip-existing salta", b.skipped === 1 && b.written === 0, JSON.stringify(b));

    // default_status
    r = await worker.fetch(req("POST", "/import?default_status=existing", {
      headers: ADMIN,
      body: { users: [{ username: "nuovo_utente", full_name: "Nuovo Utente", email: "n.u@naba.it", team: "Gamma" }] },
    }), env, ctx);
    b = await r.json();
    check("default_status applicato", b.results[0].status === "existing", JSON.stringify(b));

    // record misto valido/non valido
    r = await worker.fetch(req("POST", "/import", {
      headers: ADMIN,
      body: { users: [user({ username: "ok_utente" }), { username: "", full_name: "", email: "x", team: "" }] },
    }), env, ctx);
    b = await r.json();
    check("import parziale: 1 scritto 1 rifiutato",
      b.written === 1 && b.results.filter((x) => !x.ok).length === 1, JSON.stringify(b));
  }

  // ── 6. Export ──
  console.log("\n6. GET /export");
  {
    const env = makeEnv();
    await worker.fetch(req("POST", "/import", {
      headers: ADMIN,
      body: { users: [
        user({ username: "zeta_utente", full_name: "Zeta Utente", team: "Beta", anno_corso: "2", tesista: "no", status: "pending" }),
        user({ username: "alfa_utente", full_name: "Alfa Utente", team: "Alpha", anno_corso: "1", tesista: "no", status: "created" }),
        user({ username: "beta_utente", full_name: "Beta Utente", team: "Alpha", anno_corso: "", tesista: "yes", status: "pending" }),
        user({ username: "gamma_utente", full_name: "Gamma Utente", team: "Alpha", anno_corso: "3", tesista: "no", status: "pending" }),
      ] },
    }), env, ctx);

    let r = await worker.fetch(req("GET", "/export", { headers: ADMIN }), env, ctx);
    let b = await r.json();
    check("export conta tutti i record", b.count === 4, JSON.stringify(b.count));

    const ordine = b.users.map((u) => u.username);
    check("ordinamento team → anno → nome, tesisti in coda",
      JSON.stringify(ordine) === JSON.stringify(["alfa_utente", "gamma_utente", "beta_utente", "zeta_utente"]),
      JSON.stringify(ordine));

    r = await worker.fetch(req("GET", "/export?status=pending", { headers: ADMIN }), env, ctx);
    b = await r.json();
    check("filtro per status", b.count === 3, JSON.stringify(b.count));

    r = await worker.fetch(req("GET", "/export?format=csv", { headers: ADMIN }), env, ctx);
    const csv = await r.text();
    check("csv content-type", r.headers.get("Content-Type").includes("text/csv"));
    check("csv intestazione",
      csv.split("\n")[0] === "timestamp,username,full_name,email,team,tesista,anno_corso,status", csv.split("\n")[0]);
    check("csv righe", csv.trim().split("\n").length === 5);

    // escaping
    await worker.fetch(req("POST", "/import", {
      headers: ADMIN,
      body: { users: [user({ username: "virgola_utente", full_name: 'Rossi, Mario "il Grande"', team: "Delta" })] },
    }), env, ctx);
    r = await worker.fetch(req("GET", "/export?format=csv", { headers: ADMIN }), env, ctx);
    const csv2 = await r.text();
    check("csv fa escaping di virgole e virgolette",
      csv2.includes('"Rossi, Mario ""il Grande"""'), csv2.split("\n").find((l) => l.includes("virgola")));
  }

  // ── 7. Patch status ──
  console.log("\n7. PATCH /status");
  {
    const env = makeEnv();
    await worker.fetch(req("POST", "/import", {
      headers: ADMIN,
      body: { users: [user(), user({ team: "Beta" }), user({ username: "altro_utente", full_name: "Altro Utente", email: "a.u@naba.it", team: "Alpha" })] },
    }), env, ctx);

    let r = await worker.fetch(req("PATCH", "/status", {
      headers: ADMIN,
      body: { updates: [{ username: "mario_rossi", team: "Alpha", status: "created" }] },
    }), env, ctx);
    let b = await r.json();
    check("patch con team → 1 record", b.updated === 1, JSON.stringify(b));

    r = await worker.fetch(req("PATCH", "/status", {
      headers: ADMIN,
      body: { updates: [{ username: "mario_rossi", status: "removed" }] },
    }), env, ctx);
    b = await r.json();
    check("patch senza team → tutti i record dell'utente", b.updated === 2, JSON.stringify(b));

    r = await worker.fetch(req("GET", "/export", { headers: ADMIN }), env, ctx);
    b = await r.json();
    check("altri utenti non toccati",
      b.users.find((u) => u.username === "altro_utente").status === "pending");
    check("metadata aggiornati insieme al valore",
      b.users.filter((u) => u.username === "mario_rossi").every((u) => u.status === "removed"));

    r = await worker.fetch(req("PATCH", "/status", {
      headers: ADMIN, body: { updates: [{ username: "mario_rossi", status: "inventato" }] },
    }), env, ctx);
    b = await r.json();
    check("status non valido rifiutato", b.updated === 0 && !b.results[0].ok, JSON.stringify(b));

    r = await worker.fetch(req("PATCH", "/status", {
      headers: ADMIN, body: { updates: [{ username: "non_esiste", status: "created" }] },
    }), env, ctx);
    b = await r.json();
    check("utente inesistente segnalato", !b.results[0].ok, JSON.stringify(b));
  }

  // ── 8. Delete e purge ──
  console.log("\n8. DELETE /user e /purge");
  {
    const env = makeEnv();
    await worker.fetch(req("POST", "/import", {
      headers: ADMIN,
      body: { users: [user(), user({ team: "Beta" }), user({ username: "altro_utente", full_name: "Altro Utente", email: "a.u@naba.it", team: "Alpha" })] },
    }), env, ctx);

    let r = await worker.fetch(req("DELETE", "/user?username=mario_rossi&team=Beta", { headers: ADMIN }), env, ctx);
    let b = await r.json();
    check("delete di un singolo record", b.deleted === 1 && env.USERS.store.size === 2, JSON.stringify(b));

    r = await worker.fetch(req("DELETE", "/user?username=mario_rossi", { headers: ADMIN }), env, ctx);
    b = await r.json();
    check("delete di tutti i record dell'utente", b.deleted === 1 && env.USERS.store.size === 1, JSON.stringify(b));

    r = await worker.fetch(req("DELETE", "/user?username=non_esiste", { headers: ADMIN }), env, ctx);
    check("delete di utente inesistente → 404", r.status === 404);

    r = await worker.fetch(req("DELETE", "/user", { headers: ADMIN }), env, ctx);
    check("delete senza username → 400", r.status === 400);

    r = await worker.fetch(req("DELETE", "/purge", { headers: ADMIN }), env, ctx);
    check("purge senza conferma → 400", r.status === 400);
    check("purge senza conferma non cancella", env.USERS.store.size === 1);

    r = await worker.fetch(req("DELETE", "/purge?confirm=CONFIRM", { headers: ADMIN }), env, ctx);
    b = await r.json();
    check("purge con conferma svuota", b.deleted === 1 && env.USERS.store.size === 0, JSON.stringify(b));
  }

  // ── 9. Normalizzazione ──
  console.log("\n9. Normalizzazione dei campi");
  {
    const env = makeEnv();
    await worker.fetch(req("POST", "/import", {
      headers: ADMIN,
      body: { users: [{
        username: "  Mario_Rossi  ",
        full_name: "  Mario Rossi  ",
        email: "  MARIO.ROSSI@NABA.IT  ",
        team: "  Alpha  ",
        tesista: "YES",
        anno_corso: "7",
        status: "inventato",
        timestamp: "non-una-data",
      }] },
    }), env, ctx);

    const r = await worker.fetch(req("GET", "/export", { headers: ADMIN }), env, ctx);
    const b = await r.json();
    const u = b.users[0];
    check("spazi rimossi", u.username === "Mario_Rossi" && u.team === "Alpha", JSON.stringify(u));
    check("email in minuscolo", u.email === "mario.rossi@naba.it", u.email);
    check("tesista normalizzato", u.tesista === "yes", u.tesista);
    check("anno_corso fuori range azzerato", u.anno_corso === "", JSON.stringify(u.anno_corso));
    check("status non valido → default", u.status === "pending", u.status);
    check("timestamp non valido → adesso", !Number.isNaN(Date.parse(u.timestamp)), u.timestamp);

    // case-insensitive sulle chiavi
    await worker.fetch(req("POST", "/import", {
      headers: ADMIN,
      body: { users: [{ username: "MARIO_ROSSI", full_name: "Mario Rossi", email: "m@naba.it", team: "ALPHA" }] },
    }), env, ctx);
    check("chiavi case-insensitive: nessun doppione", env.USERS.store.size === 1, `${env.USERS.store.size} chiavi`);
  }

  // ── 10. Paginazione ──
  console.log("\n10. Paginazione oltre una pagina");
  {
    const env = makeEnv();
    const molti = Array.from({ length: 250 }, (_, i) =>
      user({ username: `utente_${String(i).padStart(3, "0")}`, full_name: `Utente ${i}`, email: `u${i}@naba.it`, team: `Team${i % 7}` }));
    for (let i = 0; i < molti.length; i += 100) {
      await worker.fetch(req("POST", "/import", { headers: ADMIN, body: { users: molti.slice(i, i + 100) } }), env, ctx);
    }
    let r = await worker.fetch(req("GET", "/export", { headers: ADMIN }), env, ctx);
    let b = await r.json();
    check("export di 250 record", b.count === 250, String(b.count));

    r = await worker.fetch(req("DELETE", "/purge?confirm=CONFIRM", { headers: ADMIN }), env, ctx);
    b = await r.json();
    check("purge di 250 record", b.deleted === 250 && env.USERS.store.size === 0, JSON.stringify(b));
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
