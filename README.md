# Ingestion Pipeline — ServiceNow → Azure AI Search (HD Oracle L2)

Pipeline di ingestion che estrae i ticket **chiusi** da ServiceNow, li filtra
**in fase di estrazione** per resolver group, li redige, ne calcola gli embedding
e li scrive in **upsert idempotente** su un indice **Azure AI Search esistente**.
L'indice è usato come *knowledge source* da un agente **Copilot Studio / Foundry**
a supporto di un help desk di secondo livello su Oracle.

> Il vincolo chiave del disegno: l'help desk deve vedere **solo** i ticket di sua
> competenza. Il filtro su `assignment_group` è applicato nella `sysparm_query`
> di ServiceNow, quindi **nell'indice entra solo ciò che è di competenza** — il
> resto non viene mai scaricato né scritto.
>
> **Nota campo `cmdb_ci`:** sull'istanza Amplifon il campo è risultato
> sistematicamente vuoto, quindi non viene indicizzato. Il *filtro* opzionale per
> configuration item resta comunque disponibile (capability dormiente,
> riattivabile via `SERVICENOW_CONFIGURATION_ITEMS`).

---

## Indice

- [Architettura e flusso](#architettura-e-flusso)
- [Struttura del progetto](#struttura-del-progetto)
- [Prerequisiti](#prerequisiti)
- [Variabili d'ambiente](#variabili-dambiente)
- [Utente di integrazione ServiceNow](#utente-di-integrazione-servicenow)
- [Schema dell'indice](#schema-dellindice)
- [Esecuzione locale](#esecuzione-locale)
- [Esecuzione in cloud (Azure Functions)](#esecuzione-in-cloud-azure-functions)
- [Modello di funzionamento](#modello-di-funzionamento)
- [Redaction](#redaction)
- [Test](#test)
- [Hardening per la produzione](#hardening-per-la-produzione)

---

## Architettura e flusso

```
ServiceNow Table API (OAuth/basic) ──▶ filtro (state, assignment_group, delta) ──▶
  trasformazione + REDACTION ──▶ embedding (Azure OpenAI / Foundry) ──▶
    ensure index (REST 2024-07-01) ──▶ merge_or_upload (upsert idempotente) ──▶
      avanzamento watermark
```

Lo stesso codice di orchestrazione (`pipeline/orchestrator.py`) è invocato sia
dall'entry point locale (`pipeline/run.py`) sia dalle Azure Functions
(`function_app.py`).

## Struttura del progetto

| File / cartella | Ruolo |
|---|---|
| `pipeline/config.py` | Configurazione da variabili d'ambiente (niente hardcoded) |
| `pipeline/servicenow.py` | Estrazione via Table API + costruzione query/filtro + retry 429 |
| `pipeline/state.py` | Watermark (Blob Storage in cloud, file JSON in locale) |
| `pipeline/redaction.py` | Redaction centralizzata e testabile |
| `pipeline/transform.py` | Record ServiceNow → documento indice |
| `pipeline/embeddings.py` | Embedding del campo `content` via Azure OpenAI |
| `pipeline/search_index.py` | Creazione indice (REST) + scrittura upsert (SDK) |
| `pipeline/orchestrator.py` | Orchestrazione del run |
| `pipeline/run.py` | Entry point locale (`python -m pipeline.run`) |
| `function_app.py` | Azure Functions: timer 02:00 + HTTP backfill |
| `tests/` | Test unitari (redaction, query/filtro, watermark, transform, schema indice, client SN) |

### Strumenti di diagnostica (eseguibili in isolamento)

| Comando | A cosa serve |
|---|---|
| `python -m pipeline.check_servicenow` | Verifica estrazione: config, query, ping, count, campione + mapping |
| `python -m pipeline.list_groups --contains Oracle,JDE` | Elenca i resolver group e i loro `sys_id` (per popolare il filtro) |
| `python -m pipeline.inspect_record [--number INC...]` | Ispeziona i campi (estesi) di un singolo incident |
| `python -m pipeline.check_embeddings` | Verifica il deployment di embedding (dimensioni vettore) |
| `python -m pipeline.check_search [--dump-schema] [--recreate]` | Verifica connessione Search, crea/ispeziona l'indice |
| `python -m pipeline.query_index --count / --search "..." / --doc INC...` | Interroga l'indice (conteggio, ricerca, lettura documento) |

## Prerequisiti

- Python 3.9+ (testato anche su 3.14; vedi nota sotto).
- Accesso a un servizio **Azure AI Search esistente** (endpoint + admin key
  oppure ruoli RBAC) — *non* viene creato dalla pipeline; viene creato solo
  l'**indice** se assente.
- Risorsa **Azure OpenAI / Foundry** con un deployment di embedding
  (`text-embedding-3-small`, 1536 dimensioni di default).
- Utente di integrazione ServiceNow **in sola lettura** (vedi sotto).
- (Solo cloud) Storage Account per il watermark e per il runtime delle Functions.
- (Solo se `SEARCH_USE_AAD=true`) **Azure CLI** con `az login` sul tenant del
  servizio Search e i ruoli RBAC necessari (vedi sotto).

Installazione dipendenze:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
pip install -r requirements.txt
```

> **Nota Python 3.14:** l'ambiente di sviluppo rilevato usa Python 3.14, molto
> recente. Se l'installazione di un pacchetto Azure fallisce per mancanza di
> wheel precompilato, aggiorna `pip` e — se necessario — crea il virtualenv con
> Python 3.11, runtime supportato anche da Azure Functions.

## Variabili d'ambiente

Copia `.env.example` in `.env` (esecuzione locale come script) **oppure**
`local.settings.json.example` in `local.settings.json` (Azure Functions Core
Tools) e valorizza le chiavi. Le principali:

| Variabile | Descrizione | Default |
|---|---|---|
| `SERVICENOW_INSTANCE` | nome istanza o URL completo | — |
| `SERVICENOW_USER` / `SERVICENOW_PASSWORD` | credenziali integrazione | — |
| `SERVICENOW_AUTH_MODE` | `basic` oppure `oauth` (password grant) | `basic` |
| `SERVICENOW_OAUTH_CLIENT_ID` / `_SECRET` / `_TOKEN_PATH` | solo se `auth_mode=oauth` | — / — / `/oauth_token.do` |
| `SERVICENOW_TABLE` | tabella sorgente | `incident` |
| `SERVICENOW_CLOSED_STATES` | stati "chiuso" (CSV) | `6,7` |
| `SERVICENOW_RESOLVER_GROUPS` | sys_id `assignment_group` (CSV) — **opzionale** | (vuoto = tutti) |
| `SERVICENOW_CONFIGURATION_ITEMS` | sys_id `cmdb_ci` (CSV) — **opzionale** | (vuoto = tutti) |
| `SERVICENOW_PAGE_SIZE` | dimensione pagina Table API | `200` |
| `WATERMARK_OVERLAP_MINUTES` | finestra di sovrapposizione in lettura | `15` |
| `SEARCH_ENDPOINT` / `SEARCH_INDEX_NAME` | Azure AI Search esistente + nome indice | — |
| `SEARCH_USE_AAD` | `true` = Azure AD (RBAC); `false` = admin key | `false` |
| `SEARCH_ADMIN_KEY` | admin key (richiesta se `SEARCH_USE_AAD=false`) | — |
| `SEARCH_API_VERSION` | api-version REST per la creazione indice | `2024-07-01` |
| `AOAI_ENDPOINT` / `AOAI_API_KEY` / `AOAI_DEPLOYMENT` | Azure OpenAI/Foundry embedding | — |
| `AOAI_MODEL` / `AOAI_DIMENSIONS` | modello e dimensioni vettore | `text-embedding-3-small` / `1536` |
| `WATERMARK_BLOB_CONNECTION_STRING` | se presente → Blob; se vuoto → file locale | (vuoto in dev) |
| `WATERMARK_LOCAL_PATH` | percorso watermark locale | `.state/watermark.json` |

> **Filtri opzionali:** `SERVICENOW_RESOLVER_GROUPS` e
> `SERVICENOW_CONFIGURATION_ITEMS` se lasciati vuoti **non aggiungono clausole**
> e si estrae tutto. Per restringere il perimetro dell'help desk, valorizzarli
> con i `sys_id` (non i nomi). La guardia di sicurezza che *pretende* i filtri è
> attivabile a livello di codice con `require_filters=True`, oppure nella
> diagnostica con `python -m pipeline.check_servicenow --require-filters`.
>
> **Autenticazione Search:** in RBAC puro (servizio senza API key) imposta
> `SEARCH_USE_AAD=true` e usa `az login` + i ruoli **Search Service Contributor**
> (creazione indice) e **Search Index Data Contributor** (scrittura). In
> alternativa abilita le API key sul servizio (modalità "Both") e usa
> `SEARCH_ADMIN_KEY`.

I valori forniti (sys_id dei gruppi, endpoint/key di Search e Azure OpenAI,
credenziali ServiceNow) vanno inseriti al posto dei placeholder `<...>` nei file
di esempio.

## Utente di integrazione ServiceNow

- Crea un utente **dedicato, in sola lettura**, con ACL ristrette alla sola
  tabella sorgente (`incident`) e ai soli campi necessari (vedi `sysparm_fields`).
- **Autenticazione:** supportate sia **basic auth** sia **OAuth 2.0 password
  grant** (endpoint `oauth_token.do`). Con OAuth il token viene rinnovato
  automaticamente su HTTP 401. Configurazione via `SERVICENOW_AUTH_MODE`.
- **Timezone = GMT.** La pipeline usa `sys_updated_on` con `display_value=all`:
  il `value` raw è in GMT solo se l'utente di integrazione ha il fuso orario
  impostato su **GMT**. Questo rende coerente la comparazione del watermark.
- Il filtro delta usa `gs.dateGenerate(...)`, che interpreta la data nel fuso
  dell'utente di integrazione: con utente GMT la comparazione è deterministica.
- A protezione del bordo, in lettura si applica una **finestra di sovrapposizione**
  (`WATERMARK_OVERLAP_MINUTES`, default 15'): si rilegge un piccolo intervallo
  già processato; l'upsert idempotente rende l'operazione innocua.

## Schema dell'indice

Creato via REST (`api-version 2024-07-01`) con corpo JSON esplicito (vedi
`pipeline/search_index.py`), per non dipendere dai nomi delle classi dell'SDK.

| Campo | Tipo | Note |
|---|---|---|
| `id` | `Edm.String` | **chiave**, derivata dal numero ticket ripulito |
| `number` | `Edm.String` | recuperabile (citazione) |
| `short_description` | `Edm.String` | searchable, titolo semantico |
| `description` | `Edm.String` | searchable |
| `resolution` | `Edm.String` | searchable (da `close_notes`, rediretto) |
| `work_notes` | `Edm.String` | searchable, **non** recuperabile (note interne, già in `content`) |
| `comments` | `Edm.String` | searchable, **non** recuperabile (commenti cliente, già in `content`) |
| `content` | `Edm.String` | searchable, contenuto principale: problema+descrizione+risoluzione+journal |
| `assignment_group` | `Edm.String` | filterable |
| `assignment_group_name` | `Edm.String` | searchable / filterable / facetable, keyword semantica, citazione |
| `priority` | `Edm.String` | filterable / facetable (es. "4 - Low") |
| `impact` | `Edm.String` | filterable / facetable |
| `urgency` | `Edm.String` | filterable / facetable |
| `closed_at` | `Edm.DateTimeOffset` | filterable / sortable, ISO 8601 con suffisso `Z` |
| `contentVector` | `Collection(Edm.Single)` | searchable, **non** recuperabile, profilo vettoriale |

- **Contenuto arricchito:** il campo `content` concatena problema, descrizione,
  risoluzione e i **journal field** (`work_notes`, `comments`), dove di norma sta
  la vera conoscenza risolutiva. I journal sono *searchable* ma **non
  recuperabili** (non esposti grezzi nelle citazioni).
- **Vector search:** algoritmo **HNSW** (metrica cosine) + profilo con
  **vectorizer `azureOpenAI`** che punta allo *stesso* modello/deployment usato
  in ingestion. Così Copilot Studio può inviare il **testo** della query e
  l'embedding della query viene calcolato **dentro** Azure AI Search.
- **Semantic search:** titolo = `short_description`, contenuto prioritario =
  `content`, keywords = `assignment_group_name`.
- Lo schema è allineato a ciò che Copilot Studio si aspetta da una knowledge
  source Azure AI Search: un campo contenuto principale (`content`) e campi
  recuperabili utili alle citazioni (`number`, `assignment_group_name`,
  `closed_at`).

## Esecuzione locale

Primo **full load** (nessun watermark presente → scarica tutto lo storico
filtrato; in alternativa forza con `--full`):

```powershell
# con il file .env valorizzato
python -m pipeline.run --full
```

Run **delta** (default — usa il watermark salvato):

```powershell
python -m pipeline.run
```

**Backfill** manuale da una data (GMT, formato ServiceNow `YYYY-MM-DD HH:MM:SS`,
non aggiorna il watermark):

```powershell
python -m pipeline.run --backfill-from "2024-01-01 00:00:00"
```

**Smoke test** end-to-end su pochi record (valida l'intera catena senza scaricare
tutto lo storico — *non* usare in produzione):

```powershell
python -m pipeline.run --full --max-records 10
```

Log più verboso: aggiungi `-v`.

### Sequenza consigliata al primo setup

```powershell
python -m pipeline.check_servicenow          # estrazione OK?
python -m pipeline.check_embeddings          # embedding OK?
python -m pipeline.check_search              # connessione + crea indice
python -m pipeline.run --full --max-records 10   # mini run end-to-end
python -m pipeline.query_index --count       # verifica documenti scritti
```

## Esecuzione in cloud (Azure Functions)

Struttura mantenuta in parallelo per il passaggio immediato in cloud (default
operativo attuale = locale):

- `nightly_delta` — **timer trigger** alle **02:00** (`0 0 2 * * *`): run delta.
- `manual_backfill` — **HTTP trigger** (`/api/backfill?full=true` oppure
  `?backfill_from=YYYY-MM-DD HH:MM:SS`).

In cloud il watermark va su **Blob Storage** (valorizza
`WATERMARK_BLOB_CONNECTION_STRING` o riusa `AzureWebJobsStorage`); in locale,
fallback automatico al file JSON.

Avvio locale dell'host Functions (opzionale):

```powershell
func start
```

## Modello di funzionamento

1. **Primo run (full load):** nessun watermark → estrae tutto lo storico filtrato.
2. **Run successivi (delta):** estrae solo i ticket con
   `sys_updated_on >= (watermark − overlap)`.
3. **Upsert idempotente** con chiave = numero ticket (ripulito): riaperture e
   modifiche alle note dopo la chiusura aggiornano il documento esistente, senza
   duplicati.
4. A fine run vengono loggate le **statistiche**: letti, trasformati, saltati,
   scritti, watermark precedente e nuovo.

## Redaction

I ticket HD Oracle contengono spesso credenziali nelle note. La redaction
(`pipeline/redaction.py`) è **centralizzata, sempre applicata prima di embedding
e scrittura**, e testabile. Pattern mascherati di default:

- `password=...`, `pwd=...`, `pass: ...`
- clausole Oracle `IDENTIFIED BY ...` (incluso `IDENTIFIED BY VALUES '...'`)
- stringhe di connessione `utente/password@host:porta/servizio` (maschera la sola
  password, mantiene utente/host/servizio per il contesto)

Per aggiungere pattern (PII, ecc.) basta estendere `DEFAULT_PATTERNS` o passare
una lista di `RedactionRule` custom a `Redactor`.

## Test

```powershell
pip install pytest
python -m pytest
```

Coperti: **redaction**, **costruzione query/filtro** (inclusi filtri opzionali),
gestione del **watermark**, **trasformazione** (con journal field e severità),
**client ServiceNow** (paginazione, retry 429, OAuth + refresh su 401) e **schema
dell'indice** (campi, chiave, vectorizer, semantic config, auth key/AAD).

## Hardening per la produzione

- **Niente chiavi nel codice/app settings in chiaro:** spostare admin key di
  Search, API key di Azure OpenAI e credenziali ServiceNow in **Azure Key Vault**
  e referenziarle (Key Vault references) o leggerle a runtime.
- **Managed Identity** al posto delle chiavi:
  - Azure AI Search: la pipeline **già supporta** Azure AD via `SEARCH_USE_AAD=true`
    (`DefaultAzureCredential`). In produzione assegnare alla managed identity i
    ruoli *Search Index Data Contributor* (scrittura) e *Search Service
    Contributor* (creazione indice), evitando l'admin key.
  - Vectorizer e embedding: configurare il vectorizer `azureOpenAI` con
    `authIdentity` (managed identity) invece di `apiKey`.
  - Blob watermark: `DefaultAzureCredential` invece della connection string.
- **ServiceNow:** l'autenticazione **OAuth** è già implementata
  (`SERVICENOW_AUTH_MODE=oauth`); in produzione conservare client secret e
  password in Key Vault.
- Restringere le **ACL** dell'utente di integrazione e limitare i `sysparm_fields`.
- **Privacy/PII:** i `comments` indicizzati possono contenere nomi di persone;
  valutare se aggiungere pattern di redaction PII o escludere il campo.
- Abilitare **Application Insights** e alert sui fallimenti del run notturno.
