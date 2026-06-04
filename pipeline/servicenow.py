"""Estrazione da ServiceNow via Table API REST.

- Costruisce il filtro direttamente nella `sysparm_query` (stato chiuso,
  assignment_group nei resolver group, cmdb_ci nei CI, delta su sys_updated_on).
- Limita i campi con `sysparm_fields`.
- `sysparm_display_value=all` per avere sia sys_id sia nome leggibile.
- Pagina con limit/offset finche' la pagina e' piena.
- Retry con backoff esponenziale su HTTP 429 (e 5xx).
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Iterator, List, Optional

import requests

from .config import ServiceNowConfig

logger = logging.getLogger(__name__)

# Campi scaricati dalla Table API.
# Oltre ai campi base del brief includiamo i journal field (work_notes,
# comments) che contengono la vera conoscenza risolutiva, e i metadati di
# severita' (priority/impact/urgency).
DEFAULT_FIELDS = [
    "sys_id",              # per costruire il link diretto all'incident
    "number",
    "short_description",
    "description",
    "close_notes",
    "work_notes",          # note interne tecniche (il "come" si risolve)
    "comments",            # additional comments (dialogo col cliente)
    "assignment_group",
    "priority",
    "impact",
    "urgency",
    "closed_at",
    "resolved_at",
    "sys_updated_on",
]


class AuthProvider:
    """Punto di estensione per l'autenticazione.

    Implementazione di default: basic auth. Per passare a OAuth in futuro basta
    fornire un AuthProvider che imposti l'header Authorization (Bearer token).
    """

    def apply(self, session: requests.Session) -> None:  # pragma: no cover - interfaccia
        raise NotImplementedError


class BasicAuthProvider(AuthProvider):
    def __init__(self, user: str, password: str) -> None:
        self._auth = (user, password)

    def apply(self, session: requests.Session) -> None:
        session.auth = self._auth


class OAuthPasswordAuthProvider(AuthProvider):
    """OAuth 2.0 'password grant' di ServiceNow (endpoint oauth_token.do).

    Recupera un access token con username/password + client_id/client_secret e
    lo imposta come header Authorization Bearer. Espone refresh_token e
    re-login per rinnovare il token quando scade (gestito dal client su 401).
    """

    def __init__(
        self,
        base_url: str,
        user: str,
        password: str,
        client_id: str,
        client_secret: str,
        token_path: str = "/oauth_token.do",
        timeout: int = 60,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.user = user
        self.password = password
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_path = token_path if token_path.startswith("/") else "/" + token_path
        self.timeout = timeout
        self._session: Optional[requests.Session] = None
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None

    @property
    def token_url(self) -> str:
        return f"{self.base_url}{self.token_path}"

    def _fetch_token(self, use_refresh: bool = False) -> None:
        if use_refresh and self._refresh_token:
            data = {
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }
        else:
            data = {
                "grant_type": "password",
                "username": self.user,
                "password": self.password,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }
        # Richiesta token senza l'header Authorization della sessione applicativa.
        response = requests.post(self.token_url, data=data, timeout=self.timeout)
        if response.status_code != 200 and use_refresh:
            # Refresh fallito: ritenta con password grant.
            logger.info("Refresh token fallito, nuovo login con password grant")
            return self._fetch_token(use_refresh=False)
        response.raise_for_status()
        payload = response.json()
        self._access_token = payload["access_token"]
        self._refresh_token = payload.get("refresh_token", self._refresh_token)
        if self._session is not None:
            self._session.headers["Authorization"] = f"Bearer {self._access_token}"
        logger.info("OAuth: access token ottenuto da %s", self.token_url)

    def apply(self, session: requests.Session) -> None:
        self._session = session
        if self._access_token is None:
            self._fetch_token(use_refresh=False)
        else:
            session.headers["Authorization"] = f"Bearer {self._access_token}"

    def refresh(self) -> None:
        """Rinnova il token (chiamato dal client su HTTP 401)."""
        self._fetch_token(use_refresh=True)


def _quote_list(values: List[str]) -> str:
    """ServiceNow IN clause: valori separati da virgola, senza spazi."""
    return ",".join(values)


def build_query(config: ServiceNowConfig, watermark: Optional[str] = None) -> str:
    """Costruisce la `sysparm_query` encoded.

    `watermark` (se presente) e' una stringa datetime ServiceNow GMT nel formato
    'YYYY-MM-DD HH:mm:ss' gia' arretrata della finestra di sovrapposizione.
    Usiamo gs.dateGenerate cosi' la comparazione avviene nel timezone
    dell'utente di integrazione (impostato a GMT), in modo deterministico.

    I filtri su resolver group e configuration item sono opzionali: se le liste
    sono vuote la relativa clausola non viene aggiunta e si estrae tutto.
    """
    clauses: List[str] = [
        f"stateIN{_quote_list(config.closed_states)}",
    ]
    if config.resolver_groups:
        clauses.append(f"assignment_groupIN{_quote_list(config.resolver_groups)}")
    if config.configuration_items:
        clauses.append(f"cmdb_ciIN{_quote_list(config.configuration_items)}")
    if watermark:
        date_part, _, time_part = watermark.partition(" ")
        time_part = time_part or "00:00:00"
        clauses.append(
            f"sys_updated_on>=javascript:gs.dateGenerate('{date_part}','{time_part}')"
        )
    # Ordinamento crescente per sys_updated_on (per watermark robusto).
    query = "^".join(clauses) + "^ORDERBYsys_updated_on"
    return query


class ServiceNowClient:
    def __init__(
        self,
        config: ServiceNowConfig,
        auth_provider: Optional[AuthProvider] = None,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.config = config
        self.session = session or requests.Session()
        self._auth_provider = auth_provider or self._default_auth_provider()
        self._auth_provider.apply(self.session)
        self.session.headers.update(
            {"Accept": "application/json", "Content-Type": "application/json"}
        )

    def _default_auth_provider(self) -> AuthProvider:
        mode = (self.config.auth_mode or "basic").lower()
        if mode == "basic":
            return BasicAuthProvider(self.config.user, self.config.password)
        if mode == "oauth":
            if not self.config.oauth_client_id or not self.config.oauth_client_secret:
                raise NotImplementedError(
                    "auth_mode 'oauth' richiede SERVICENOW_OAUTH_CLIENT_ID e "
                    "SERVICENOW_OAUTH_CLIENT_SECRET."
                )
            return OAuthPasswordAuthProvider(
                base_url=self.config.base_url,
                user=self.config.user,
                password=self.config.password,
                client_id=self.config.oauth_client_id,
                client_secret=self.config.oauth_client_secret,
                token_path=self.config.oauth_token_path,
                timeout=self.config.timeout_seconds,
            )
        raise NotImplementedError(
            f"auth_mode '{self.config.auth_mode}' non supportato (usa 'basic' o 'oauth')."
        )

    def _request_page(self, params: Dict[str, str]) -> List[dict]:
        url = f"{self.config.base_url}/api/now/table/{self.config.table}"
        backoff = 1.0
        token_refreshed = False
        for attempt in range(1, self.config.max_retries + 1):
            response = self.session.get(
                url, params=params, timeout=self.config.timeout_seconds
            )
            if response.status_code == 200:
                return response.json().get("result", [])
            if (
                response.status_code == 401
                and isinstance(self._auth_provider, OAuthPasswordAuthProvider)
                and not token_refreshed
            ):
                logger.info("ServiceNow 401: rinnovo token OAuth e riprovo")
                self._auth_provider.refresh()
                token_refreshed = True
                continue
            if response.status_code == 429 or 500 <= response.status_code < 600:
                retry_after = response.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else backoff
                logger.warning(
                    "ServiceNow HTTP %s (tentativo %s/%s). Retry tra %.1fs",
                    response.status_code,
                    attempt,
                    self.config.max_retries,
                    wait,
                )
                time.sleep(wait)
                backoff = min(backoff * 2, 60.0)
                continue
            response.raise_for_status()
        raise RuntimeError(
            f"ServiceNow: superato il numero massimo di retry ({self.config.max_retries})"
        )

    def ping(self) -> int:
        """Verifica connettivita'/autenticazione con una richiesta minima.

        Ritorna lo status HTTP (200 = OK). Solleva eccezione su credenziali
        errate (401) o istanza non raggiungibile.
        """
        url = f"{self.config.base_url}/api/now/table/{self.config.table}"
        params = {"sysparm_limit": "1", "sysparm_fields": "sys_id"}
        response = self.session.get(
            url, params=params, timeout=self.config.timeout_seconds
        )
        response.raise_for_status()
        return response.status_code

    def count(self, watermark: Optional[str] = None) -> int:
        """Conta i record che rispettano i filtri, via Aggregate API.

        Non scarica i record: utile per validare il filtro prima di un full load.
        """
        query = build_query(self.config, watermark)
        url = f"{self.config.base_url}/api/now/stats/{self.config.table}"
        params = {"sysparm_query": query, "sysparm_count": "true"}
        response = self.session.get(
            url, params=params, timeout=self.config.timeout_seconds
        )
        response.raise_for_status()
        stats = response.json().get("result", {}).get("stats", {})
        return int(stats.get("count", 0))

    def iter_records(
        self,
        watermark: Optional[str] = None,
        fields: Optional[List[str]] = None,
        max_records: Optional[int] = None,
        query_override: Optional[str] = None,
    ) -> Iterator[dict]:
        """Itera i record che rispettano i filtri, paginando.

        `max_records` (opzionale) limita il numero totale di record restituiti:
        utile per smoke test senza scaricare tutto lo storico.
        `query_override` (opzionale) sostituisce la query costruita da
        build_query: utile per interrogare tabelle ausiliarie (es. gruppi).
        """
        query = query_override if query_override is not None else build_query(
            self.config, watermark
        )
        field_list = ",".join(fields or DEFAULT_FIELDS)
        offset = 0
        page_size = self.config.page_size
        if max_records is not None:
            page_size = min(page_size, max_records)
        total = 0
        logger.info("ServiceNow query: %s", query)
        while True:
            params = {
                "sysparm_query": query,
                "sysparm_fields": field_list,
                "sysparm_display_value": "all",
                "sysparm_exclude_reference_link": "true",
                "sysparm_limit": str(page_size),
                "sysparm_offset": str(offset),
            }
            page = self._request_page(params)
            if not page:
                break
            for record in page:
                yield record
                total += 1
                if max_records is not None and total >= max_records:
                    logger.info(
                        "ServiceNow: raggiunto max_records=%s, stop", max_records
                    )
                    return
            logger.info(
                "ServiceNow: pagina offset=%s size=%s (totale finora %s)",
                offset,
                len(page),
                total,
            )
            if len(page) < page_size:
                break
            offset += page_size
        logger.info("ServiceNow: estrazione completata, %s record letti", total)
