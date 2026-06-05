# Deploy della Azure Function (delta notturno)

Questa guida descrive come mettere in produzione la corsa notturna che, ogni
notte alle **02:00**, indicizza i ticket ServiceNow **chiusi/modificati** dal
giorno precedente (modalità *delta*, basata sul watermark).

La Function condivide lo **stesso codice** dell'esecuzione locale
(`pipeline/`), quindi il comportamento è identico a quello già testato.

---

## Architettura in cloud

```
Timer 02:00 ──▶ nightly_delta (Azure Function)
                   │
                   ├─ legge watermark da BLOB STORAGE
                   ├─ estrae da ServiceNow (solo > watermark)
                   ├─ redaction (regex + PII Presidio)
                   ├─ embedding (Azure OpenAI/Foundry)
                   ├─ upsert su Azure AI Search
                   └─ salva nuovo watermark su BLOB
```

Esiste anche un trigger HTTP `manual_backfill` per full load / backfill on demand.

---

## ⚠️ Punti critici (leggere prima del deploy)

### 1. Watermark su Blob Storage (obbligatorio)
Le Functions sono **stateless**: il watermark NON può stare su file locale.
Va su Blob Storage, altrimenti ogni notte ripartirebbe da zero.
→ Valorizzare `WATERMARK_BLOB_CONNECTION_STRING` (o riusare `AzureWebJobsStorage`).

### 2. Piano di hosting per la PII (Presidio + spaCy)
I modelli spaCy IT+EN pesano ~1GB. Questo **supera** i limiti del piano
**Consumption** (Y1) e causa cold start molto lunghi.
Opzioni:
- **Premium (EP1)** o **App Service plan**: consigliato se la PII è attiva.
- **Consumption SENZA PII** (`PII_REDACTION_ENABLED=false`): leggero, ma i nuovi
  ticket non avrebbero la redaction PII (sconsigliato se lo storico ce l'ha).
- **Container** (Functions su container): massima flessibilità sui modelli.

I modelli, in cloud, si installano via pip da URL (vedi `requirements.txt`,
righe commentate da decommentare per il deploy con PII).

### 3. Runtime Python
Azure Functions supporta fino a Python 3.11/3.12 (non 3.14). Per il deploy usare
un ambiente Python 3.11. Il codice è compatibile.

### 4. Segreti
NON deployare `.env` (già escluso da `.funcignore`). I valori vanno nelle
**Application Settings** della Function App (meglio se via Key Vault reference).
In hardening: managed identity per Search/OpenAI/Blob al posto delle chiavi.

---

## Comandi di deploy (az CLI + Azure Functions Core Tools)

Prerequisiti: `az login` sul tenant/subscription corretti, `func` (Core Tools v4),
ambiente Python 3.11.

```bash
# Variabili
RG="rg-oracle-hd"
LOC="westeurope"
STORAGE="storaclehd$RANDOM"
APP="func-oracle-hd-indexer"
PLAN="plan-oracle-hd"          # solo se Premium/App Service

# 1. Resource group
az group create -n $RG -l $LOC

# 2. Storage (richiesto dalle Functions + watermark)
az storage account create -n $STORAGE -g $RG -l $LOC --sku Standard_LRS

# 3a. Piano Premium EP1 (consigliato con PII)
az functionapp plan create -g $RG -n $PLAN --location $LOC --sku EP1 --is-linux
az functionapp create -g $RG -n $APP --storage-account $STORAGE \
  --plan $PLAN --runtime python --runtime-version 3.11 \
  --functions-version 4 --os-type Linux

# 3b. (Alternativa) Consumption SENZA PII
# az functionapp create -g $RG -n $APP --storage-account $STORAGE \
#   --consumption-plan-location $LOC --runtime python --runtime-version 3.11 \
#   --functions-version 4 --os-type Linux

# 4. Application Settings (sostituire i placeholder con i valori reali)
az functionapp config appsettings set -g $RG -n $APP --settings \
  SERVICENOW_INSTANCE="amplifongroup" \
  SERVICENOW_AUTH_MODE="oauth" \
  SERVICENOW_USER="<utente>" \
  SERVICENOW_PASSWORD="<password>" \
  SERVICENOW_OAUTH_CLIENT_ID="<client_id>" \
  SERVICENOW_OAUTH_CLIENT_SECRET="<client_secret>" \
  SERVICENOW_RESOLVER_GROUPS="<csv sys_id>" \
  SEARCH_ENDPOINT="https://altea-agents-hd-snow.search.windows.net" \
  SEARCH_INDEX_NAME="oracle-hd-kb" \
  SEARCH_ADMIN_KEY="<admin_key>" \
  AOAI_ENDPOINT="https://altea-agents-hd-resource.services.ai.azure.com" \
  AOAI_API_KEY="<aoai_key>" \
  AOAI_DEPLOYMENT="text-embedding-3-small" \
  WATERMARK_BLOB_CONNECTION_STRING="<connection_string_storage>" \
  PII_REDACTION_ENABLED="true"

# 5. Deploy del codice (dalla cartella di progetto)
func azure functionapp publish $APP --python
```

> Per il deploy CON PII, prima decommentare le due righe dei modelli spaCy in
> `requirements.txt`.

---

## Verifica post-deploy

- Portale Azure → Function App → **Functions**: devono comparire `nightly_delta`
  e `manual_backfill`.
- Test immediato del backfill HTTP (richiede la function key):
  `https://<APP>.azurewebsites.net/api/backfill?full=false` (delta on demand).
- **Application Insights** → Logs: verificare "Run delta completato" dopo le 02:00.
- Controllare il Blob `ingestion-state/watermark.json` che avanzi ogni notte.

---

## Allineamento orario con il watermark

Il watermark usa `sys_updated_on` in GMT. L'utente di integrazione ServiceNow
deve avere timezone **GMT** (vedi README). La finestra di sovrapposizione
(`WATERMARK_OVERLAP_MINUTES`, default 15') protegge i record al bordo.
