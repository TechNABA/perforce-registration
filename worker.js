/**
 * worker.js — Backend di registrazione Perforce NABA
 *
 * Sostituisce il vecchio proxy verso GitHub Actions. Tutti i dati personali
 * vivono in Cloudflare KV, mai nella repo pubblica.
 *
 * Endpoint:
 *   GET    /          → health check (pubblico, nessun dato)
 *   POST   /          → registrazione dal form (pubblico)
 *   POST   /import    → upsert bulk (admin)
 *   GET    /export    → dump completo, json o csv (admin)
 *   PATCH  /status    → aggiornamento status (admin)
 *   DELETE /user      → rimuove i record di un utente (admin)
 *   DELETE /purge     → svuota il KV (admin)
 *
 * Binding richiesto:  USERS           (KV namespace)
 * Secret richiesti:   ADMIN_TOKEN     (autenticazione endpoint admin)
 *                     DISCORD_WEBHOOK (notifica registrazioni, opzionale)
 *
 * I log del Worker non contengono mai nomi, email o team.
 */

const ALLOWED_ORIGINS = [
  "https://p4setup.naba.it",
  "https://technaba.github.io",
];

const FIELDS = [
  "timestamp", "username", "full_name", "email",
  "team", "tesista", "anno_corso", "status",
];

const VALID_STATUS = new Set([
  "pending", "created", "existing", "removed", "duplicate", "error",
]);

// Il form manda al massimo un tesista + i membri del gruppo.
const MAX_BATCH_PUBLIC = 25;
const MAX_BATCH_ADMIN = 500;

const KEY_PREFIX = "user:";

// ── Chiavi KV ─────────────────────────────────────────────────
// user:{username}#{team} — una chiave per coppia utente/team, come le righe
// del vecchio CSV. encodeURIComponent tiene il separatore '#' non ambiguo.
function kvKey(username, team) {
  return KEY_PREFIX + enc(username) + "#" + enc(team);
}

function userPrefix(username) {
  return KEY_PREFIX + enc(username) + "#";
}

function enc(value) {
  return encodeURIComponent(String(value ?? "").trim().toLowerCase());
}

// ── Risposte ──────────────────────────────────────────────────
function json(body, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
      ...extraHeaders,
    },
  });
}

function corsHeaders(request) {
  const origin = request.headers.get("Origin") || "";
  if (!ALLOWED_ORIGINS.includes(origin)) return {};
  return {
    "Access-Control-Allow-Origin": origin,
    "Vary": "Origin",
  };
}

// ── Autenticazione ────────────────────────────────────────────
// Confronto a tempo costante: evita di far dedurre il token byte per byte.
function timingSafeEqual(a, b) {
  const bufA = new TextEncoder().encode(a);
  const bufB = new TextEncoder().encode(b);
  if (bufA.length !== bufB.length) return false;
  let diff = 0;
  for (let i = 0; i < bufA.length; i++) diff |= bufA[i] ^ bufB[i];
  return diff === 0;
}

function isAdmin(request, env) {
  const expected = env.ADMIN_TOKEN;
  if (!expected) return false;
  const provided = request.headers.get("X-Admin-Token") || "";
  if (!provided) return false;
  return timingSafeEqual(provided, expected);
}

// ── Validazione ───────────────────────────────────────────────
const RE_USERNAME = /^[A-Za-z0-9._-]{2,64}$/;
const RE_EMAIL = /^[^@\s]+@[^@\s.]+\.[^@\s]+$/;
const RE_TEAM = /^[A-Za-z0-9 ._-]{1,64}$/;

function clean(value, maxLen) {
  // Via i caratteri di controllo: finirebbero nel CSV di export.
  return String(value ?? "")
    .replace(/[\u0000-\u001F\u007F]/g, "")
    .trim()
    .slice(0, maxLen);
}

/** Normalizza e valida un record. Ritorna {record} oppure {error}. */
function normalizeUser(raw, { defaultStatus = "pending" } = {}) {
  if (!raw || typeof raw !== "object") return { error: "record non valido" };

  const username = clean(raw.username, 64);
  const fullName = clean(raw.full_name, 120);
  const email = clean(raw.email, 200).toLowerCase();
  const team = clean(raw.team, 64);

  if (!RE_USERNAME.test(username)) return { error: "username non valido" };
  if (!fullName) return { error: "full_name mancante" };
  if (!RE_EMAIL.test(email)) return { error: "email non valida" };
  if (!RE_TEAM.test(team)) return { error: "team non valido" };

  const tesista = clean(raw.tesista, 3).toLowerCase();
  const annoCorso = clean(raw.anno_corso, 1);
  let status = clean(raw.status, 16).toLowerCase() || defaultStatus;
  if (!VALID_STATUS.has(status)) status = defaultStatus;

  let timestamp = clean(raw.timestamp, 40);
  if (!timestamp || Number.isNaN(Date.parse(timestamp))) {
    timestamp = new Date().toISOString();
  }

  return {
    record: {
      timestamp,
      username,
      full_name: fullName,
      email,
      team,
      tesista: tesista === "yes" ? "yes" : tesista === "no" ? "no" : "",
      anno_corso: ["1", "2", "3"].includes(annoCorso) ? annoCorso : "",
      status,
    },
  };
}

// ── Accesso al KV ─────────────────────────────────────────────
/**
 * Elenca tutti i record. Il record completo sta anche nei metadata della
 * chiave, così list() basta da solo: niente N+1 get su 200 utenti.
 */
async function listAllUsers(env) {
  const out = [];
  let cursor;

  do {
    const page = await env.USERS.list({ prefix: KEY_PREFIX, cursor });
    const missing = [];

    for (const key of page.keys) {
      if (key.metadata && key.metadata.username) {
        out.push(key.metadata);
      } else {
        missing.push(key.name); // record scritto prima dei metadata
      }
    }

    if (missing.length) {
      const fetched = await Promise.all(
        missing.map((name) => env.USERS.get(name, { type: "json" }))
      );
      for (const record of fetched) if (record) out.push(record);
    }

    cursor = page.list_complete ? undefined : page.cursor;
  } while (cursor);

  return out;
}

async function putUser(env, record) {
  await env.USERS.put(kvKey(record.username, record.team), JSON.stringify(record), {
    metadata: record,
  });
}

async function keysForUsername(env, username) {
  const prefix = userPrefix(username);
  const names = [];
  let cursor;

  do {
    const page = await env.USERS.list({ prefix, cursor });
    for (const key of page.keys) names.push(key.name);
    cursor = page.list_complete ? undefined : page.cursor;
  } while (cursor);

  return names;
}

// ── CSV ───────────────────────────────────────────────────────
function csvEscape(value) {
  const s = String(value ?? "");
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function toCsv(records) {
  const lines = [FIELDS.join(",")];
  for (const r of records) {
    lines.push(FIELDS.map((f) => csvEscape(r[f])).join(","));
  }
  return lines.join("\n") + "\n";
}

// Ordinamento storico dell'export: team → anno_corso → full_name,
// con i tesisti (anno_corso vuoto) in coda al proprio team.
function sortRecords(records) {
  return records.sort((a, b) => {
    const teamA = (a.team || "").toLowerCase();
    const teamB = (b.team || "").toLowerCase();
    if (teamA !== teamB) return teamA < teamB ? -1 : 1;

    const annoA = /^\d+$/.test(a.anno_corso || "") ? Number(a.anno_corso) : 99;
    const annoB = /^\d+$/.test(b.anno_corso || "") ? Number(b.anno_corso) : 99;
    if (annoA !== annoB) return annoA - annoB;

    const nameA = (a.full_name || "").toLowerCase();
    const nameB = (b.full_name || "").toLowerCase();
    return nameA < nameB ? -1 : nameA > nameB ? 1 : 0;
  });
}

// ── Notifica Discord ──────────────────────────────────────────
async function notifyDiscord(env, records) {
  const webhook = env.DISCORD_WEBHOOK;
  if (!webhook || !records.length) return;

  const lines = records.map(
    (r) => `• **${r.full_name}** (\`${r.username}\`) — ${r.team}`
  );

  const payload = {
    embeds: [
      {
        title: "📋 Nuova registrazione Perforce",
        description: lines.join("\n").slice(0, 4000),
        color: 2664261,
        fields: [
          { name: "Utenti", value: String(records.length), inline: true },
          { name: "Team", value: records[0].team || "N/A", inline: true },
          {
            name: "Tesista",
            value: (records[0].tesista || "no").replace(/^./, (c) => c.toUpperCase()),
            inline: true,
          },
        ],
        footer: { text: "Perforce NABA Registration System" },
      },
    ],
  };

  try {
    // Senza User-Agent custom Cloudflare risponde 403 (error code 1010).
    const resp = await fetch(webhook, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "User-Agent": "NABA-Perforce-Bot/1.0",
      },
      body: JSON.stringify(payload),
    });
    if (!resp.ok) console.log(`discord webhook: HTTP ${resp.status}`);
  } catch (err) {
    console.log(`discord webhook: ${err.message}`);
  }
}

// ── Handler: POST / (pubblico) ────────────────────────────────
async function handleRegister(request, env, ctx) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ success: false, error: "JSON non valido" }, 400, corsHeaders(request));
  }

  const incoming = Array.isArray(body) ? body : body?.users;
  if (!Array.isArray(incoming) || incoming.length === 0) {
    return json({ success: false, error: "nessun utente nella richiesta" }, 400, corsHeaders(request));
  }
  if (incoming.length > MAX_BATCH_PUBLIC) {
    return json(
      { success: false, error: `massimo ${MAX_BATCH_PUBLIC} utenti per richiesta` },
      400,
      corsHeaders(request)
    );
  }

  const results = [];
  const stored = [];

  for (const raw of incoming) {
    const { record, error } = normalizeUser(raw, { defaultStatus: "pending" });
    if (error) {
      results.push({ username: clean(raw?.username, 64), ok: false, error });
      continue;
    }

    const key = kvKey(record.username, record.team);
    const existingSameTeam = await env.USERS.get(key);
    if (existingSameTeam) {
      // Già registrato per questo team: non sovrascrivere il record originale.
      results.push({ username: record.username, ok: true, status: "already_exists" });
      continue;
    }

    // Username già presente su un altro team: si registra comunque, ma
    // flaggato 'duplicate' perché l'admin lo riveda.
    const others = await keysForUsername(env, record.username);
    if (others.length > 0) record.status = "duplicate";

    await putUser(env, record);
    stored.push(record);
    results.push({ username: record.username, ok: true, status: record.status });
  }

  if (stored.length) {
    ctx.waitUntil(notifyDiscord(env, stored));
  }

  const failed = results.filter((r) => !r.ok);
  if (stored.length === 0 && failed.length > 0) {
    return json(
      { success: false, error: failed[0].error, results },
      400,
      corsHeaders(request)
    );
  }

  console.log(`register: ${stored.length} stored, ${failed.length} rejected`);
  return json({ success: true, stored: stored.length, results }, 200, corsHeaders(request));
}

// ── Handler: POST /import (admin) ─────────────────────────────
async function handleImport(request, env) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ success: false, error: "JSON non valido" }, 400);
  }

  const incoming = Array.isArray(body) ? body : body?.users;
  if (!Array.isArray(incoming) || incoming.length === 0) {
    return json({ success: false, error: "nessun utente nella richiesta" }, 400);
  }
  if (incoming.length > MAX_BATCH_ADMIN) {
    return json({ success: false, error: `massimo ${MAX_BATCH_ADMIN} utenti per richiesta` }, 400);
  }

  const url = new URL(request.url);
  const skipExisting = url.searchParams.get("mode") === "skip-existing";
  const defaultStatus = clean(url.searchParams.get("default_status"), 16) || "pending";

  const results = [];
  let written = 0;
  let skipped = 0;

  for (const raw of incoming) {
    const { record, error } = normalizeUser(raw, {
      defaultStatus: VALID_STATUS.has(defaultStatus) ? defaultStatus : "pending",
    });
    if (error) {
      results.push({ username: clean(raw?.username, 64), ok: false, error });
      continue;
    }

    if (skipExisting) {
      const existing = await env.USERS.get(kvKey(record.username, record.team));
      if (existing) {
        skipped++;
        results.push({ username: record.username, ok: true, status: "skipped" });
        continue;
      }
    }

    await putUser(env, record);
    written++;
    results.push({ username: record.username, ok: true, status: record.status });
  }

  console.log(`import: ${written} written, ${skipped} skipped`);
  return json({ success: true, written, skipped, results });
}

// ── Handler: GET /export (admin) ──────────────────────────────
async function handleExport(request, env) {
  const url = new URL(request.url);
  const format = (url.searchParams.get("format") || "json").toLowerCase();
  const statusFilter = clean(url.searchParams.get("status"), 16).toLowerCase();

  let records = await listAllUsers(env);
  if (statusFilter) {
    records = records.filter((r) => (r.status || "").toLowerCase() === statusFilter);
  }
  sortRecords(records);

  console.log(`export: ${records.length} records, format=${format}`);

  if (format === "csv") {
    return new Response(toCsv(records), {
      headers: {
        "Content-Type": "text/csv; charset=utf-8",
        "Content-Disposition": 'attachment; filename="users.csv"',
        "Cache-Control": "no-store",
      },
    });
  }

  return json({ success: true, count: records.length, users: records });
}

// ── Handler: PATCH /status (admin) ────────────────────────────
async function handleStatus(request, env) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ success: false, error: "JSON non valido" }, 400);
  }

  const updates = Array.isArray(body) ? body : body?.updates;
  if (!Array.isArray(updates) || updates.length === 0) {
    return json({ success: false, error: "nessun aggiornamento nella richiesta" }, 400);
  }
  if (updates.length > MAX_BATCH_ADMIN) {
    return json({ success: false, error: `massimo ${MAX_BATCH_ADMIN} aggiornamenti per richiesta` }, 400);
  }

  const results = [];
  let updated = 0;

  for (const item of updates) {
    const username = clean(item?.username, 64);
    const team = clean(item?.team, 64);
    const status = clean(item?.status, 16).toLowerCase();

    if (!RE_USERNAME.test(username)) {
      results.push({ username, ok: false, error: "username non valido" });
      continue;
    }
    if (!VALID_STATUS.has(status)) {
      results.push({ username, ok: false, error: `status non valido: ${status}` });
      continue;
    }

    // Senza team si aggiornano tutti i record dell'utente.
    const keys = team ? [kvKey(username, team)] : await keysForUsername(env, username);
    if (keys.length === 0) {
      results.push({ username, ok: false, error: "utente non trovato" });
      continue;
    }

    let touched = 0;
    for (const key of keys) {
      const record = await env.USERS.get(key, { type: "json" });
      if (!record) continue;
      record.status = status;
      await env.USERS.put(key, JSON.stringify(record), { metadata: record });
      touched++;
    }

    if (touched === 0) {
      results.push({ username, ok: false, error: "utente non trovato" });
    } else {
      updated += touched;
      results.push({ username, ok: true, updated: touched, status });
    }
  }

  console.log(`status: ${updated} records updated`);
  return json({ success: true, updated, results });
}

// ── Handler: DELETE /user (admin) ─────────────────────────────
async function handleDeleteUser(request, env) {
  const url = new URL(request.url);
  const username = clean(url.searchParams.get("username"), 64);
  const team = clean(url.searchParams.get("team"), 64);

  if (!RE_USERNAME.test(username)) {
    return json({ success: false, error: "parametro username mancante o non valido" }, 400);
  }

  // Senza team si cancellano tutti i record dell'utente.
  const keys = team ? [kvKey(username, team)] : await keysForUsername(env, username);

  let deleted = 0;
  for (const key of keys) {
    if (await env.USERS.get(key)) {
      await env.USERS.delete(key);
      deleted++;
    }
  }

  console.log(`delete: ${deleted} records removed`);
  if (deleted === 0) {
    return json({ success: false, error: "utente non trovato", deleted: 0 }, 404);
  }
  return json({ success: true, deleted });
}

// ── Handler: DELETE /purge (admin) ────────────────────────────
async function handlePurge(request, env) {
  const url = new URL(request.url);
  if (url.searchParams.get("confirm") !== "CONFIRM") {
    return json(
      { success: false, error: "aggiungi ?confirm=CONFIRM per svuotare il KV" },
      400
    );
  }

  // Si rilegge sempre la prima pagina: un cursore su una lista che si sta
  // svuotando può saltare chiavi.
  let deleted = 0;

  for (let round = 0; round < 50; round++) {
    const page = await env.USERS.list({ prefix: KEY_PREFIX });
    if (page.keys.length === 0) break;
    await Promise.all(page.keys.map((k) => env.USERS.delete(k.name)));
    deleted += page.keys.length;
  }

  console.log(`purge: ${deleted} records deleted`);
  return json({ success: true, deleted });
}

// ── Router ────────────────────────────────────────────────────
export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";
    const method = request.method.toUpperCase();

    // Preflight per il form del sito.
    if (method === "OPTIONS") {
      const cors = corsHeaders(request);
      if (!cors["Access-Control-Allow-Origin"]) {
        return new Response(null, { status: 403 });
      }
      return new Response(null, {
        status: 204,
        headers: {
          ...cors,
          "Access-Control-Allow-Methods": "POST, OPTIONS",
          "Access-Control-Allow-Headers": "Content-Type",
          "Access-Control-Max-Age": "86400",
        },
      });
    }

    if (!env.USERS) {
      console.log("config: KV binding USERS mancante");
      return json({ success: false, error: "backend non configurato" }, 500);
    }

    try {
      // Pubblici
      if (path === "/" && method === "GET") {
        return json({ ok: true, service: "naba-perforce-registration" });
      }
      if (path === "/" && method === "POST") {
        return await handleRegister(request, env, ctx);
      }

      // Admin
      const adminRoutes = {
        "POST /import": handleImport,
        "GET /export": handleExport,
        "PATCH /status": handleStatus,
        "DELETE /user": handleDeleteUser,
        "DELETE /purge": handlePurge,
      };

      const handler = adminRoutes[`${method} ${path}`];
      if (handler) {
        if (!isAdmin(request, env)) {
          console.log(`auth: rifiutato ${method} ${path}`);
          return json({ success: false, error: "non autorizzato" }, 401);
        }
        return await handler(request, env);
      }

      return json({ success: false, error: "endpoint non trovato" }, 404);
    } catch (err) {
      // Mai il messaggio grezzo al client: potrebbe contenere dati del record.
      console.log(`error: ${method} ${path} — ${err.message}`);
      return json({ success: false, error: "errore interno" }, 500);
    }
  },
};
