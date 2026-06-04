"""Indice Azure AI Search: creazione (REST) e scrittura (SDK).

- La creazione dell'indice usa la REST API con corpo JSON esplicito
  (api-version 2024-07-01) per non dipendere dai nomi delle classi dell'SDK,
  che cambiano tra versioni.
- La scrittura usa `merge_or_upload_documents` dell'SDK azure-search-documents
  (upsert idempotente, stabile tra versioni), a batch.
- Vector search HNSW + vectorizer azureOpenAI (Copilot Studio manda il testo
  della query: l'embedding della query avviene dentro Azure AI Search).
- Semantic search configurata (titolo, contenuto, keywords).
"""

from __future__ import annotations

import logging
from typing import Dict, List

import requests

from .config import EmbeddingConfig, SearchConfig

logger = logging.getLogger(__name__)

VECTOR_PROFILE = "hnsw-aoai-profile"
VECTOR_ALGORITHM = "hnsw-algo"
VECTORIZER_NAME = "aoai-vectorizer"
SEMANTIC_CONFIG = "semantic-config"


def build_index_definition(index_name: str, embedding: EmbeddingConfig) -> Dict:
    """Corpo JSON esplicito per la creazione/aggiornamento dell'indice."""
    return {
        "name": index_name,
        "fields": [
            {
                "name": "id",
                "type": "Edm.String",
                "key": True,
                "searchable": False,
                "filterable": True,
                "sortable": False,
                "facetable": False,
            },
            {
                "name": "number",
                "type": "Edm.String",
                "searchable": True,
                "filterable": True,
                "sortable": True,
                "retrievable": True,
            },
            {
                # URL diretto all'incident, per le citazioni. Non searchable.
                "name": "url",
                "type": "Edm.String",
                "searchable": False,
                "retrievable": True,
            },
            {
                "name": "short_description",
                "type": "Edm.String",
                "searchable": True,
                "retrievable": True,
            },
            {
                "name": "description",
                "type": "Edm.String",
                "searchable": True,
                "retrievable": True,
            },
            {
                "name": "resolution",
                "type": "Edm.String",
                "searchable": True,
                "retrievable": True,
            },
            {
                # Note interne tecniche: searchable ma NON recuperabili
                # (gia' incluse in content; evitiamo di esporle nelle citazioni).
                "name": "work_notes",
                "type": "Edm.String",
                "searchable": True,
                "retrievable": False,
            },
            {
                # Commenti col cliente: searchable ma non recuperabili.
                "name": "comments",
                "type": "Edm.String",
                "searchable": True,
                "retrievable": False,
            },
            {
                "name": "content",
                "type": "Edm.String",
                "searchable": True,
                "retrievable": True,
            },
            {
                "name": "assignment_group",
                "type": "Edm.String",
                "filterable": True,
                "retrievable": True,
            },
            {
                "name": "assignment_group_name",
                "type": "Edm.String",
                "searchable": True,
                "filterable": True,
                "facetable": True,
                "retrievable": True,
            },
            {
                "name": "priority",
                "type": "Edm.String",
                "filterable": True,
                "facetable": True,
                "retrievable": True,
            },
            {
                "name": "impact",
                "type": "Edm.String",
                "filterable": True,
                "facetable": True,
                "retrievable": True,
            },
            {
                "name": "urgency",
                "type": "Edm.String",
                "filterable": True,
                "facetable": True,
                "retrievable": True,
            },
            {
                "name": "closed_at",
                "type": "Edm.DateTimeOffset",
                "filterable": True,
                "sortable": True,
                "retrievable": True,
            },
            {
                "name": "contentVector",
                "type": "Collection(Edm.Single)",
                "searchable": True,
                "retrievable": False,
                "dimensions": embedding.dimensions,
                "vectorSearchProfile": VECTOR_PROFILE,
            },
        ],
        "vectorSearch": {
            "algorithms": [
                {
                    "name": VECTOR_ALGORITHM,
                    "kind": "hnsw",
                    "hnswParameters": {
                        "m": 4,
                        "efConstruction": 400,
                        "efSearch": 500,
                        "metric": "cosine",
                    },
                }
            ],
            "vectorizers": [
                {
                    "name": VECTORIZER_NAME,
                    "kind": "azureOpenAI",
                    "azureOpenAIParameters": {
                        "resourceUri": embedding.endpoint,
                        "deploymentId": embedding.deployment,
                        "modelName": embedding.model,
                        "apiKey": embedding.api_key,
                    },
                }
            ],
            "profiles": [
                {
                    "name": VECTOR_PROFILE,
                    "algorithm": VECTOR_ALGORITHM,
                    "vectorizer": VECTORIZER_NAME,
                }
            ],
        },
        "semantic": {
            "configurations": [
                {
                    "name": SEMANTIC_CONFIG,
                    "prioritizedFields": {
                        "titleField": {"fieldName": "short_description"},
                        "prioritizedContentFields": [{"fieldName": "content"}],
                        "prioritizedKeywordsFields": [
                            {"fieldName": "assignment_group_name"}
                        ],
                    },
                }
            ]
        },
    }


# Scope OAuth per il control plane dati di Azure AI Search.
SEARCH_AAD_SCOPE = "https://search.azure.com/.default"


class SearchIndexManager:
    def __init__(
        self, search: SearchConfig, embedding: EmbeddingConfig, credential=None
    ) -> None:
        self.search = search
        self.embedding = embedding
        self._credential = credential
        if search.use_aad and self._credential is None:
            from azure.identity import DefaultAzureCredential

            self._credential = DefaultAzureCredential()

    def _build_headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.search.use_aad:
            token = self._credential.get_token(SEARCH_AAD_SCOPE).token
            headers["Authorization"] = f"Bearer {token}"
        else:
            headers["api-key"] = self.search.admin_key
        return headers

    def _index_url(self) -> str:
        return (
            f"{self.search.endpoint.rstrip('/')}/indexes/{self.search.index_name}"
            f"?api-version={self.search.api_version}"
        )

    def index_exists(self) -> bool:
        resp = requests.get(self._index_url(), headers=self._build_headers(), timeout=60)
        if resp.status_code == 200:
            return True
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        return False

    def ensure_index(self) -> None:
        """Crea l'indice se non esiste (PUT con corpo esplicito)."""
        if self.index_exists():
            logger.info("Indice '%s' gia' presente", self.search.index_name)
            return
        definition = build_index_definition(self.search.index_name, self.embedding)
        resp = requests.put(
            self._index_url(), headers=self._build_headers(), json=definition, timeout=60
        )
        if resp.status_code not in (200, 201):
            logger.error("Creazione indice fallita: %s %s", resp.status_code, resp.text)
            resp.raise_for_status()
        logger.info("Indice '%s' creato", self.search.index_name)

    def update_index(self) -> None:
        """Aggiorna lo schema dell'indice (PUT) creandolo se assente.

        Azure AI Search consente di AGGIUNGERE nuovi campi a un indice esistente
        senza re-indicizzare (i campi gia' presenti non vanno rimossi/modificati
        in modo incompatibile). Utile quando lo schema evolve, es. nuovo campo
        `url`. I documenti esistenti avranno il nuovo campo vuoto finche' non
        vengono riscritti dalla pipeline.
        """
        definition = build_index_definition(self.search.index_name, self.embedding)
        resp = requests.put(
            self._index_url(), headers=self._build_headers(), json=definition, timeout=60
        )
        # 200/201 = creato/aggiornato con corpo; 204 = aggiornato senza corpo.
        if resp.status_code not in (200, 201, 204):
            logger.error("Aggiornamento indice fallito: %s %s", resp.status_code, resp.text)
            resp.raise_for_status()
        logger.info("Indice '%s' aggiornato (schema)", self.search.index_name)


class SearchWriter:
    """Scrittura upsert idempotente via SDK (merge_or_upload_documents)."""

    def __init__(self, search: SearchConfig, client=None, credential=None) -> None:
        self.search = search
        if client is not None:
            self._client = client
        else:
            from azure.search.documents import SearchClient

            if search.use_aad:
                if credential is None:
                    from azure.identity import DefaultAzureCredential

                    credential = DefaultAzureCredential()
                cred = credential
            else:
                from azure.core.credentials import AzureKeyCredential

                cred = AzureKeyCredential(search.admin_key)

            self._client = SearchClient(
                endpoint=search.endpoint,
                index_name=search.index_name,
                credential=cred,
            )

    def upsert(self, documents: List[dict]) -> int:
        """Scrive i documenti a batch. Ritorna il numero di documenti scritti."""
        if not documents:
            return 0
        written = 0
        batch_size = self.search.write_batch_size
        for start in range(0, len(documents), batch_size):
            batch = documents[start : start + batch_size]
            results = self._client.merge_or_upload_documents(documents=batch)
            failed = [r for r in results if not r.succeeded]
            if failed:
                for r in failed:
                    logger.error(
                        "Scrittura fallita key=%s status=%s error=%s",
                        getattr(r, "key", "?"),
                        getattr(r, "status_code", "?"),
                        getattr(r, "error_message", "?"),
                    )
                raise RuntimeError(
                    f"{len(failed)} documenti non scritti su Azure AI Search"
                )
            written += len(batch)
            logger.info("Search: scritti %s/%s documenti", written, len(documents))
        return written
