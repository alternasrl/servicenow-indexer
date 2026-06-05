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
import os
from concurrent.futures import ThreadPoolExecutor
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
    pii_occurrences_masked: int = 0
    pii_values_masked: int = 0
    pii_docs_with_pii: int = 0

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "read": self.read,
            "transformed": self.transformed,
            "skipped_empty": self.skipped_empty,
            "written": self.written,
            "previous_watermark": self.previous_watermark,
            "new_watermark": self.new_watermark,
            "pii_occurrences_masked": self.pii_occurrences_masked,
            "pii_values_masked": self.pii_values_masked,
            "pii_docs_with_pii": self.pii_docs_with_pii,
        }


def _default_redactor() -> Redactor:
    """Redactor con PII agganciata da env (se PII_REDACTION_ENABLED=true)."""
    from .redaction import _build_default_redactor

    return _build_default_redactor()


@dataclass
class Pipeline:
    config: AppConfig
    redactor: Redactor = field(default_factory=_default_redactor)
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

    def _apply_pii(self, documents: List[dict]) -> None:
        """Applica la redaction PII ai documenti, parallelizzata.

        Per minimizzare le chiamate LLM si redige il SOLO campo `content` (1
        chiamata per ticket): contiene gia' problema+descrizione+risoluzione+
        journal, ed e' il campo vettorizzato e mostrato nelle citazioni. Gli
        altri campi testuali sono resi non-recuperabili nello schema indice,
        quindi non espongono PII grezza. No-op se la PII non e' attiva."""
        pii = getattr(self.redactor, "pii_redactor", None)
        if pii is None or not documents:
            return
        # Concorrenza piu' prudente di default: troppi thread saturano il rate
        # limit del deployment (tempesta di 429). Sovrascrivibile via env.
        workers = int(os.environ.get("PII_CONCURRENCY", "8"))

        # Pre-inizializza il client PRIMA del ThreadPool: il lazy-init concorrente
        # serializzerebbe il primo batch di thread (ognuno crea il client).
        ensure = getattr(pii, "_ensure_client", None)
        if callable(ensure):
            try:
                ensure()
            except Exception:  # pragma: no cover
                pass

        total = len(documents)
        done = {"n": 0}
        lock_done = __import__("threading").Lock()
        step = max(1, total // 20)  # log ~ogni 5%

        def redact_doc(doc: dict) -> bool:
            """Ritorna True se ok, False se la redaction e' fallita."""
            ok = True
            val = doc.get("content")
            if val:
                try:
                    doc["content"] = pii.redact(val, raise_on_error=True)
                except Exception:
                    ok = False  # gia' contato/loggato dentro redact
            with lock_done:
                done["n"] += 1
                if done["n"] % step == 0 or done["n"] == total:
                    logger.info(
                        "PII: %s/%s documenti | dati sensibili rimossi finora: %s "
                        "(in %s ticket) | falliti: %s",
                        done["n"], total, pii.occurrences_masked,
                        pii.docs_with_pii, pii.failures,
                    )
            return ok

        logger.info(
            "Redaction PII (LLM) su %s documenti, concorrenza=%s...", total, workers
        )
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(redact_doc, documents))

        # Ritento i documenti falliti (sequenziale, per non ri-saturare).
        failed = [doc for doc, ok in zip(documents, results) if not ok]
        if failed:
            logger.warning(
                "PII: %s documenti falliti al 1o passaggio, ritento in sequenza...",
                len(failed),
            )
            still_failed = 0
            for doc in failed:
                val = doc.get("content")
                if not val:
                    continue
                try:
                    doc["content"] = pii.redact(val, raise_on_error=True)
                except Exception:
                    still_failed += 1
            if still_failed:
                logger.error(
                    "ATTENZIONE: %s documenti NON redatti dalla PII anche dopo "
                    "retry. Il loro `content` potrebbe contenere dati personali.",
                    still_failed,
                )

        logger.info(
            "Redaction PII completata: %s occorrenze rimosse (%s valori, in "
            "%s/%s ticket). Fallimenti totali (cumulati): %s",
            pii.occurrences_masked, pii.values_masked, pii.docs_with_pii,
            total, pii.failures,
        )
        # Espone i contatori per le statistiche finali del run.
        self._pii_stats = dict(pii.stats)

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

        # 3) Estrazione + trasformazione (SOLO regex; la PII e' una fase a parte,
        #    parallelizzata, perche' con backend LLM e' la fase piu' lenta).
        regex_only = Redactor(rules=self.redactor.rules)  # niente PII qui
        documents: List[dict] = []
        new_watermark = stats.previous_watermark
        base_url = self.config.servicenow.base_url
        for record in self.sn_client.iter_records(
            watermark=read_watermark, max_records=max_records
        ):
            stats.read += 1
            doc = transform_record(record, regex_only, base_url=base_url)
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

        # 3bis) Redaction PII (se attiva) - parallela sui campi recuperabili.
        self._apply_pii(documents)
        pii_stats = getattr(self, "_pii_stats", None)
        if pii_stats:
            stats.pii_occurrences_masked = pii_stats.get("occurrences_masked", 0)
            stats.pii_values_masked = pii_stats.get("values_masked", 0)
            stats.pii_docs_with_pii = pii_stats.get("docs_with_pii", 0)

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
