"""Orchestrazione del run di ingestion.

Coordina: lettura watermark -> estrazione ServiceNow -> trasformazione +
redaction -> embedding -> creazione indice -> upsert -> salvataggio watermark.

Modalita':
- full load: ignora il watermark, estrae tutto lo storico filtrato.
- delta: estrae solo i record modificati dopo (watermark - overlap).
- backfill: full load a partire da una data fornita.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from .config import AppConfig
from .embeddings import EmbeddingClient
from .redaction import Redactor
from .search_index import SearchIndexManager, SearchWriter
from .servicenow import ServiceNowClient
from .state import (
    WatermarkStore,
    apply_overlap,
    build_watermark_store,
    max_watermark,
)
from .transform import record_updated_on, transform_record

logger = logging.getLogger(__name__)


@dataclass
class RunStats:
    read: int = 0
    transformed: int = 0
    skipped_empty: int = 0
    written: int = 0
    previous_watermark: Optional[str] = None
    new_watermark: Optional[str] = None
    mode: str = "delta"

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "read": self.read,
            "transformed": self.transformed,
            "skipped_empty": self.skipped_empty,
            "written": self.written,
            "previous_watermark": self.previous_watermark,
            "new_watermark": self.new_watermark,
        }


@dataclass
class Pipeline:
    config: AppConfig
    redactor: Redactor = field(default_factory=Redactor)
    _sn_client: Optional[ServiceNowClient] = None
    _embed_client: Optional[EmbeddingClient] = None
    _writer: Optional[SearchWriter] = None
    _index_manager: Optional[SearchIndexManager] = None
    _watermark_store: Optional[WatermarkStore] = None

    # --- factory lazy (sovrascrivibili nei test) ---
    @property
    def sn_client(self) -> ServiceNowClient:
        if self._sn_client is None:
            self._sn_client = ServiceNowClient(self.config.servicenow)
        return self._sn_client

    @property
    def embed_client(self) -> EmbeddingClient:
        if self._embed_client is None:
            self._embed_client = EmbeddingClient(self.config.embedding)
        return self._embed_client

    @property
    def writer(self) -> SearchWriter:
        if self._writer is None:
            self._writer = SearchWriter(self.config.search)
        return self._writer

    @property
    def index_manager(self) -> SearchIndexManager:
        if self._index_manager is None:
            self._index_manager = SearchIndexManager(
                self.config.search, self.config.embedding
            )
        return self._index_manager

    @property
    def watermark_store(self) -> WatermarkStore:
        if self._watermark_store is None:
            self._watermark_store = build_watermark_store(self.config.state)
        return self._watermark_store

    def run(
        self,
        full: bool = False,
        backfill_from: Optional[str] = None,
        max_records: Optional[int] = None,
    ) -> RunStats:
        stats = RunStats()

        # 1) Determina il watermark di partenza.
        if full:
            stats.mode = "full"
            read_watermark = None
            logger.info("Run FULL LOAD: watermark ignorato")
        elif backfill_from:
            stats.mode = "backfill"
            read_watermark = backfill_from
            logger.info("Run BACKFILL da %s", backfill_from)
        else:
            stats.mode = "delta"
            stored = self.watermark_store.read()
            stats.previous_watermark = stored
            read_watermark = apply_overlap(stored, self.config.servicenow.overlap_minutes)
            if stored is None:
                stats.mode = "full"
                logger.info("Nessun watermark presente: primo run = FULL LOAD")
            else:
                logger.info(
                    "Run DELTA: watermark=%s, con overlap=%s -> query>=%s",
                    stored,
                    self.config.servicenow.overlap_minutes,
                    read_watermark,
                )

        # 2) Assicura l'indice (creazione se non esiste).
        self.index_manager.ensure_index()

        # 3) Estrazione + trasformazione.
        documents: List[dict] = []
        new_watermark = stats.previous_watermark
        base_url = self.config.servicenow.base_url
        for record in self.sn_client.iter_records(
            watermark=read_watermark, max_records=max_records
        ):
            stats.read += 1
            doc = transform_record(record, self.redactor, base_url=base_url)
            updated_on = record_updated_on(record)
            new_watermark = max_watermark(new_watermark, updated_on)
            if not doc.get("content"):
                stats.skipped_empty += 1
                logger.debug("Ticket %s senza content: saltato", doc.get("number"))
                continue
            documents.append(doc)
            stats.transformed += 1

        logger.info(
            "Estrazione: %s letti, %s trasformati, %s saltati (content vuoto)",
            stats.read,
            stats.transformed,
            stats.skipped_empty,
        )

        if not documents:
            logger.info("Nessun documento da scrivere")
            stats.new_watermark = new_watermark
            if new_watermark and stats.mode != "backfill":
                self.watermark_store.write(new_watermark)
            return stats

        # 4) Embedding.
        documents = self.embed_client.embed_documents(documents)
        # Dopo l'embedding, eventuali documenti senza vettore non vengono scritti.
        to_write = [d for d in documents if d.get("contentVector")]

        # 5) Scrittura upsert.
        stats.written = self.writer.upsert(to_write)

        # 6) Avanzamento watermark (massimo visto nel run).
        stats.new_watermark = new_watermark
        if new_watermark and stats.mode != "backfill":
            self.watermark_store.write(new_watermark)

        logger.info("Run completato: %s", stats.as_dict())
        return stats
