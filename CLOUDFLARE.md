# Guida all'interfaccia Cloudflare

Procedura completa per configurare e deployare il Worker dalla dashboard.
Ogni passo presuppone il precedente.

**Prima di iniziare:** finché non completi il passo 5 il sistema vecchio
continua a funzionare. Non toccare il PAT GitHub né fare push delle modifiche
alla repo prima di aver verificato il nuovo Worker.

---

## 0. Orientarsi nella dashboard

Vai su **dash.cloudflare.com** e fai login.

Se hai più account, la prima schermata è un elenco: scegli quello che contiene
il Worker. Il nome dell'account è sempre visibile in alto a sinistra.

Nella barra laterale sinistra ti servono due voci:

- **Compute (Workers)** — dove vive il Worker
- **Storage & Databases** — dove vivono i namespace KV

> Cloudflare rinomina spesso queste voci. In dashboard più vecchie sono
> raggruppate sotto un'unica voce **Workers & Pages**, con il KV in una tab
> interna. Se i nomi non corrispondono, cerca per funzione: il KV è sempre
> sotto la sezione di storage, il Worker sotto quella di compute.

---

## 1. Creare il namespace KV

Il namespace è il contenitore dei dati. Va creato prima del Worker, perché
il Worker deve poterlo agganciare.

1. Barra laterale → **Storage & Databases** → **KV**
2. Bottone **Create** (o **Create a namespace**) in alto a destra
3. Campo **Namespace Name**: scrivi `perforce-users`
4. **Add** / **Create**

Torni all'elenco dei namespace e vedi `perforce-users` con un **ID** accanto,
una stringa esadecimale lunga. Non ti serve copiarlo se deployi dalla
dashboard; serve solo se un giorno usi `wrangler`, e in quel caso va in
[`wrangler.toml`](wrangler.toml).

Il namespace ora è vuoto: è normale.

---

## 2. Collegare il KV al Worker (binding)

Il binding è il nome con cui il codice vede il namespace. Il codice cerca
esattamente `USERS`: se lo chiami diversamente il Worker risponde 500.

1. Barra laterale → **Compute (Workers)** → clicca su **perforce-registration**
2. Tab **Settings**
3. Sezione **Bindings** → bottone **Add**
4. Scegli il tipo **KV namespace**
5. Compila:
   - **Variable name**: `USERS` ← maiuscolo, esattamente così
   - **KV namespace**: seleziona `perforce-users` dal menu
6. **Deploy** / **Save**

Il binding compare nell'elenco. Se scrivi `Users` o `USER` il Worker non
trova niente: il confronto è case-sensitive.

---

## 3. Impostare i secret

Stessa pagina **Settings**, sezione **Variables and Secrets**.

La differenza fra i due tipi conta: una **Variable** resta leggibile in chiaro
da chiunque abbia accesso alla dashboard, un **Secret** dopo il salvataggio
non è più visibile a nessuno, nemmeno a te. Entrambi vanno creati come
**Secret**.

Per ognuno: **Add** → **Type: Secret** → compila → **Deploy**.

| Variable name | Valore |
|---|---|
| `ADMIN_TOKEN` | la stringa in `Documents\NABA-perforce-backup\ADMIN_TOKEN.txt` |
| `DISCORD_WEBHOOK` | l'URL che oggi sta nei GitHub Secrets come `DISCORD_WEBHOOK_URL` |

Per recuperare il webhook Discord: GitHub → repo → **Settings** →
**Secrets and variables** → **Actions**. Da lì il valore non è più leggibile,
quindi prendilo da dove l'hai generato: Discord → **Impostazioni server** →
**Integrazioni** → **Webhook** → il tuo webhook → **Copia URL webhook**.

**Non toccare ancora** `GITHUB_PAT`, `GITHUB_OWNER`, `GITHUB_REPO`: si
rimuovono al passo 7, quando sei sicuro che il nuovo Worker funziona.

---

## 4. Deployare il codice

1. Sempre in **perforce-registration**, bottone **Edit code** in alto a destra
   (in alcune versioni è sotto un menu **···** o nella tab **Deployments**)
2. Si apre l'editor: a sinistra i file, al centro il codice, a destra un
   pannello di anteprima
3. Apri [`worker.js`](worker.js) sul tuo computer, seleziona tutto, copia
4. Nell'editor Cloudflare seleziona tutto il contenuto esistente e incolla sopra
5. Bottone **Deploy** in alto a destra → conferma

Se il codice ha errori di sintassi l'editor te lo dice prima del deploy e il
deploy non parte. Il codice attuale è già stato verificato con Node, quindi
non dovrebbe succedere.

Il deploy richiede pochi secondi. Da questo momento le nuove registrazioni
vanno nel KV e non più su GitHub.

---

## 5. Verificare

Dal terminale, nella cartella del progetto:

```bash
npm test
```

Esegue la suite del Worker in locale (59 controlli su routing, autenticazione,
validazione, storage, paginazione). Non tocca Cloudflare: verifica il codice,
non il deploy.

Per verificare il deploy vero:

```bash
python scripts/kv_status.py
```

Ti chiede l'`ADMIN_TOKEN` e stampa cosa c'è nel KV. Le tre risposte possibili:

| Risposta | Significato |
|---|---|
| `Record nel KV: 0` | tutto a posto, il KV è vuoto perché non hai ancora migrato |
| errore 401 | l'`ADMIN_TOKEN` sul Worker non coincide con quello che hai inserito |
| errore 500 sul binding | il binding non si chiama `USERS`, torna al passo 2 |

Per vedere cosa succede in tempo reale: **Compute (Workers)** →
**perforce-registration** → tab **Logs** → **Begin log stream**. Lascia aperto
e rilancia il comando: vedi arrivare le richieste. I log contengono solo
conteggi e path, mai nomi o email.

---

## 6. Migrare i dati

Prima un controllo a vuoto, che non scrive niente:

```bash
python scripts/migrate_to_kv.py --dry-run
```

Legge il CSV dal backup in `Documents\NABA-perforce-backup\users.csv` e stampa
un riepilogo per status e per team. Attesi 14 record: 13 `existing` e 1 `pending`.

Se il riepilogo torna:

```bash
python scripts/migrate_to_kv.py
```

Verifica finale:

```bash
python scripts/kv_status.py --usernames
```

Devono risultare 14 record.

> Il KV è *eventually consistent*: dopo una scrittura la lettura può restare
> indietro di qualche decina di secondi. Gli script riprovano da soli. Se
> subito dopo la migrazione vedi meno record del previsto, aspetta un minuto
> e rilancia `kv_status.py` prima di preoccuparti.

Per vedere i dati nella dashboard: **Storage & Databases** → **KV** →
`perforce-users` → tab **Metrics** per i grafici, oppure **View** per sfogliare
le chiavi. Sono nella forma `user:{username}#{team}`.

---

## 7. Ripulire

Solo dopo che il passo 6 è andato a buon fine.

**Su Cloudflare** — Settings → Variables and Secrets → rimuovi
`GITHUB_PAT`, `GITHUB_OWNER`, `GITHUB_REPO` (icona cestino accanto a ognuno) → **Deploy**

**Su GitHub:**

- **Settings** → **Developer settings** → **Personal access tokens** →
  **Tokens (classic)** → trova il token del progetto → **Delete**.
  Farlo *dopo* aver rimosso le variabili dal Worker, non prima.
- Repo → **Settings** → **Secrets and variables** → **Actions** →
  rimuovi `DISCORD_WEBHOOK_URL`
- Repo → **Settings** → **Actions** → **General** → imposta
  **Actions permissions** su **Disable actions**: non ci sono più workflow

**History git** — ✅ **già fatto.** `git filter-repo --path data/ --invert-paths`
è stato eseguito: `data/` non è più in nessun commit e i commit che toccavano
solo quei file sono spariti.

Due conseguenze da conoscere:

- **Il remote è stato rimosso.** `filter-repo` lo toglie apposta, per evitare
  push accidentali su una history riscritta. Va riaggiunto:

  ```bash
  git remote add origin https://github.com/TechNABA/perforce-registration.git
  ```

- **`filter-repo` fa un `reset --hard` finale.** Qualunque modifica non
  committata al momento dell'esecuzione viene persa. Committa *prima* di
  rilanciarlo, se mai servisse di nuovo.

Il push va fatto forzato, perché la history locale e quella remota sono
ormai divergenti:

```bash
git push origin --force --all
```

**Log delle Actions** — i vecchi run contengono nomi e team nei log, visibili
pubblicamente. Vanno cancellati: repo → tab **Actions** → seleziona il workflow
→ per ogni run il menu **···** → **Delete workflow run**. Con molti run conviene
l'API:

```bash
gh api repos/TechNABA/perforce-registration/actions/runs --paginate --jq '.workflow_runs[].id' | xargs -I {} gh api -X DELETE repos/TechNABA/perforce-registration/actions/runs/{}
```

---

## Uso quotidiano

```bash
python scripts/kv_status.py                            # cosa c'è nel KV
python scripts/kv_status.py --xlsx utenti.xlsx         # XLSX formattato
python scripts/perforce_provision.py --dry-run         # anteprima provisioning
python scripts/perforce_provision.py                   # provisioning completo
python scripts/export_p4_users.py                      # Perforce → KV
python scripts/perforce_prune.py --dry-run             # account orfani
```

Tutti chiedono l'`ADMIN_TOKEN` a runtime. Per non riscriverlo a ogni comando,
una volta per sessione di terminale:

```bash
$env:NABA_ADMIN_TOKEN = 'il-token'
```

Gli XLSX e i CSV generati contengono dati personali. `.gitignore` li esclude,
ma tienili comunque fuori dalla repo.

---

## Se qualcosa va storto

| Sintomo | Causa | Rimedio |
|---|---|---|
| Il form dice "Failed to fetch" | Worker non raggiungibile o CORS | Logs del Worker; l'origine del sito deve essere in `ALLOWED_ORIGINS` in `worker.js` |
| 401 su tutti i comandi | `ADMIN_TOKEN` diverso | Reimposta il secret e rifai Deploy |
| 500 "backend non configurato" | Binding assente o con nome sbagliato | Passo 2, la variabile deve chiamarsi `USERS` |
| Nessuna notifica Discord | `DISCORD_WEBHOOK` assente o URL rigenerato | Ricopia l'URL dal server Discord |
| Il KV sembra vuoto dopo una scrittura | Eventual consistency | Aspetta un minuto e rilancia |
| Registrazione respinta | Validazione lato Worker | I log dicono quanti record sono stati rifiutati; la risposta al client dice il motivo |

**Tornare indietro a un deploy precedente:** **Compute (Workers)** →
**perforce-registration** → tab **Deployments** → trova la versione buona →
menu **···** → **Rollback**. È istantaneo e non tocca il KV.

**Svuotare il KV** (irreversibile, cancella tutti i record):

```bash
curl -X DELETE "https://perforce-registration.tech-0a4.workers.dev/purge?confirm=CONFIRM" -H "X-Admin-Token: il-token"
```

---

## Endpoint del Worker

| Metodo | Path | Auth | Funzione |
|---|---|---|---|
| `GET` | `/` | — | Health check |
| `POST` | `/` | — | Registrazione dal form, notifica Discord |
| `POST` | `/import` | `X-Admin-Token` | Upsert bulk, max 500 per richiesta |
| `GET` | `/export` | `X-Admin-Token` | Dump completo, `?format=csv\|json`, `?status=` |
| `PATCH` | `/status` | `X-Admin-Token` | Aggiorna status |
| `DELETE` | `/user` | `X-Admin-Token` | Rimuove un utente, `?username=`, `?team=` |
| `DELETE` | `/purge` | `X-Admin-Token` | Svuota il KV, richiede `?confirm=CONFIRM` |

Chiavi KV: `user:{username}#{team}`, una per coppia utente/team. Il record
completo sta anche nei metadata della chiave, così l'export non deve fare una
`get()` per ogni utente.
