"""Configurazione della pipeline.

Tutti i parametri sono letti da variabili d'ambiente: niente valori hardcoded.
Per l'esecuzione locale e' sufficiente popolare un file `.env` (vedi
`.env.example`) oppure le App Settings di Azure Functions
(`local.settings.json` -> sezione `Values`).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

try:
    # In locale carichiamo automaticamente un eventuale file .env.
    # In cloud le variabili arrivano dalle App Settings, quindi e' un no-op.
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv e' opzionale
    pass


class ConfigError(RuntimeError):
    """Configurazione mancante o non valida."""


def _get(name: str, default: Optional[str] = None, required: bool = False) -> Optional[str]:
    value = os.environ.get(name, default)
    if required and (value is None or value.strip() == ""):
        raise ConfigError(f"Variabile d'ambiente obbligatoria mancante: {name}")
    return value


def _get_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} deve essere un intero, ricevuto: {raw!r}") from exc


def _get_list(name: str, default: Optional[str] = None) -> List[str]:
    raw = os.environ.get(name, default)
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass
class ServiceNowConfig:
    instance: str
    user: str
    password: str
    table: str = "incident"
    closed_states: List[str] = field(default_factory=lambda: ["6", "7"])
    resolver_groups: List[str] = field(default_factory=list)
    configuration_items: List[str] = field(default_factory=list)
    page_size: int = 200
    # Finestra di sovrapposizione (minuti) applicata al watermark in lettura.
    overlap_minutes: int = 15
    # Auth: 'basic' oppure 'oauth' (password grant via oauth_token.do).
    auth_mode: str = "basic"
    # Parametri OAuth (usati solo se auth_mode == 'oauth').
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    oauth_token_path: str = "/oauth_token.do"
    timeout_seconds: int = 60
    max_retries: int = 5

    @property
    def base_url(self) -> str:
        host = self.instance
        if not host.startswith("http://") and not host.startswith("https://"):
            host = f"https://{host}.service-now.com"
        return host.rstrip("/")

    @staticmethod
    def from_env(require_filters: bool = False) -> "ServiceNowConfig":
        """Carica la sola configurazione ServiceNow dalle variabili d'ambiente.

        Utile per testare l'estrazione in isolamento, senza dipendere dalla
        configurazione di Azure AI Search / Azure OpenAI.

        I filtri su resolver group e configuration item sono **opzionali**: se
        non valorizzati non viene aggiunta la relativa clausola e si estrae
        tutto. `require_filters=True` ripristina la guardia di sicurezza (errore
        se i filtri mancano), per scenari in cui non si vuole rischiare di
        indicizzare ticket fuori competenza.
        """
        cfg = ServiceNowConfig(
            instance=_get("SERVICENOW_INSTANCE", required=True),
            user=_get("SERVICENOW_USER", required=True),
            password=_get("SERVICENOW_PASSWORD", required=True),
            table=_get("SERVICENOW_TABLE", "incident"),
            closed_states=_get_list("SERVICENOW_CLOSED_STATES", "6,7"),
            resolver_groups=_get_list("SERVICENOW_RESOLVER_GROUPS"),
            configuration_items=_get_list("SERVICENOW_CONFIGURATION_ITEMS"),
            page_size=_get_int("SERVICENOW_PAGE_SIZE", 200),
            overlap_minutes=_get_int("WATERMARK_OVERLAP_MINUTES", 15),
            auth_mode=_get("SERVICENOW_AUTH_MODE", "basic"),
            oauth_client_id=_get("SERVICENOW_OAUTH_CLIENT_ID", ""),
            oauth_client_secret=_get("SERVICENOW_OAUTH_CLIENT_SECRET", ""),
            oauth_token_path=_get("SERVICENOW_OAUTH_TOKEN_PATH", "/oauth_token.do"),
            timeout_seconds=_get_int("SERVICENOW_TIMEOUT_SECONDS", 60),
            max_retries=_get_int("SERVICENOW_MAX_RETRIES", 5),
        )
        if require_filters:
            if not cfg.resolver_groups:
                raise ConfigError(
                    "SERVICENOW_RESOLVER_GROUPS e' vuoto ma require_filters=True: "
                    "rifiuto di estrarre senza filtro sui resolver group."
                )
            if not cfg.configuration_items:
                raise ConfigError(
                    "SERVICENOW_CONFIGURATION_ITEMS e' vuoto ma require_filters="
                    "True: rifiuto di estrarre senza filtro sui configuration item."
                )
        return cfg


@dataclass
class SearchConfig:
    endpoint: str
    index_name: str
    # admin_key opzionale: se use_aad=True si usa Azure AD (DefaultAzureCredential)
    # e la chiave non serve.
    admin_key: str = ""
    use_aad: bool = False
    api_version: str = "2024-07-01"
    write_batch_size: int = 500

    @staticmethod
    def from_env() -> "SearchConfig":
        use_aad = (_get("SEARCH_USE_AAD", "false") or "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        cfg = SearchConfig(
            endpoint=_get("SEARCH_ENDPOINT", required=True),
            index_name=_get("SEARCH_INDEX_NAME", required=True),
            admin_key=_get("SEARCH_ADMIN_KEY", "") or "",
            use_aad=use_aad,
            api_version=_get("SEARCH_API_VERSION", "2024-07-01"),
            write_batch_size=_get_int("SEARCH_WRITE_BATCH_SIZE", 500),
        )
        if not cfg.use_aad and not cfg.admin_key:
            raise ConfigError(
                "Azure AI Search: serve SEARCH_ADMIN_KEY oppure SEARCH_USE_AAD=true "
                "(autenticazione via Azure AD / DefaultAzureCredential)."
            )
        return cfg


@dataclass
class EmbeddingConfig:
    endpoint: str
    api_key: str
    deployment: str
    model: str = "text-embedding-3-small"
    dimensions: int = 1536
    api_version: str = "2024-02-01"
    batch_size: int = 16
    # Limite TOKEN del testo inviato all'embedding. Il modello accetta max 8192
    # token; usiamo 8000 di margine. Il troncamento e' fatto contando i token
    # reali (tiktoken) quando disponibile, con fallback per caratteri.
    max_input_tokens: int = 8000
    # Fallback per caratteri se tiktoken non e' installato (stima ~3 char/token
    # nel caso peggiore di testo denso: SQL, codici, ID).
    max_input_chars: int = 24000

    @staticmethod
    def from_env() -> "EmbeddingConfig":
        """Carica la sola configurazione embedding (per test in isolamento)."""
        return EmbeddingConfig(
            endpoint=_get("AOAI_ENDPOINT", required=True),
            api_key=_get("AOAI_API_KEY", required=True),
            deployment=_get("AOAI_DEPLOYMENT", required=True),
            model=_get("AOAI_MODEL", "text-embedding-3-small"),
            dimensions=_get_int("AOAI_DIMENSIONS", 1536),
            api_version=_get("AOAI_API_VERSION", "2024-02-01"),
            batch_size=_get_int("AOAI_BATCH_SIZE", 16),
            max_input_tokens=_get_int("AOAI_MAX_INPUT_TOKENS", 8000),
            max_input_chars=_get_int("AOAI_MAX_INPUT_CHARS", 24000),
        )


@dataclass
class StateConfig:
    # Se la connection string blob e' assente, si usa il file locale.
    blob_connection_string: Optional[str] = None
    blob_container: str = "ingestion-state"
    blob_name: str = "watermark.json"
    local_path: str = ".state/watermark.json"


@dataclass
class AppConfig:
    servicenow: ServiceNowConfig
    search: SearchConfig
    embedding: EmbeddingConfig
    state: StateConfig

    @staticmethod
    def from_env() -> "AppConfig":
        # Filtri opzionali: se resolver group / CI non sono valorizzati si
        # estrae tutto (la guardia di sicurezza si attiva con require_filters).
        servicenow = ServiceNowConfig.from_env(require_filters=False)
        search = SearchConfig.from_env()
        embedding = EmbeddingConfig.from_env()
        state = StateConfig(
            blob_connection_string=_get("WATERMARK_BLOB_CONNECTION_STRING")
            or _get("AzureWebJobsStorage"),
            blob_container=_get("WATERMARK_BLOB_CONTAINER", "ingestion-state"),
            blob_name=_get("WATERMARK_BLOB_NAME", "watermark.json"),
            local_path=_get("WATERMARK_LOCAL_PATH", ".state/watermark.json"),
        )

        return AppConfig(
            servicenow=servicenow,
            search=search,
            embedding=embedding,
            state=state,
        )
