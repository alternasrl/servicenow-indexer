"""Embedding del campo `content` via Azure OpenAI.

- Lavora a batch.
- Salta i documenti con content vuoto (non genera vettore).
- Usa lo stesso modello/deployment che verra' configurato come vectorizer
  nell'indice Azure AI Search, cosi' query e documenti vivono nello stesso
  spazio vettoriale.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from typing import Iterable, List

from .config import EmbeddingConfig

logger = logging.getLogger(__name__)

# Watchdog: una singola chiamata embedding non puo' durare piu' di tot secondi.
# Il timeout del client httpx a volte NON scatta (socket appeso su Windows);
# questo watchdog abbandona la chiamata, ricrea il client e ritenta.
EMBED_CALL_WATCHDOG_S = 90
EMBED_MAX_ATTEMPTS = 4


class EmbeddingClient:
    def __init__(self, config: EmbeddingConfig, client=None) -> None:
        self.config = config
        self._injected = client is not None
        self._client = client or self._new_client()
        # Executor mono-thread riutilizzato per il watchdog sulle chiamate.
        self._watchdog = ThreadPoolExecutor(max_workers=1)
        # Encoder per il conteggio token reale (tiktoken). Se non disponibile,
        # si ricade sul troncamento per caratteri.
        self._encoding = None
        try:
            import tiktoken  # import lazy

            try:
                self._encoding = tiktoken.encoding_for_model(config.model)
            except KeyError:
                # Modello non mappato: cl100k_base copre gli embedding moderni.
                self._encoding = tiktoken.get_encoding("cl100k_base")
        except Exception:  # pragma: no cover - tiktoken opzionale
            logger.warning(
                "tiktoken non disponibile: troncamento embedding per caratteri "
                "(meno preciso)."
            )

    def _new_client(self):
        from openai import AzureOpenAI  # import lazy

        return AzureOpenAI(
            azure_endpoint=self.config.endpoint,
            api_key=self.config.api_key,
            api_version=self.config.api_version,
            timeout=60.0,
            max_retries=5,
        )

    def _truncate(self, text: str) -> str:
        """Tronca il testo al limite di token ammesso dall'embedding.

        Il modello accetta max 8192 token in input. Con tiktoken si tronca al
        numero di token reale (preciso); altrimenti si ricade sul limite di
        caratteri. Il content completo resta nell'indice: si limita solo l'input
        embedding.
        """
        if self._encoding is not None:
            tokens = self._encoding.encode(text)
            if len(tokens) > self.config.max_input_tokens:
                return self._encoding.decode(tokens[: self.config.max_input_tokens])
            return text
        limit = self.config.max_input_chars
        if limit and len(text) > limit:
            return text[:limit]
        return text

    def _raw_embed(self, inputs: List[str]):
        return self._client.embeddings.create(
            model=self.config.deployment,
            input=inputs,
            dimensions=self.config.dimensions,
        )

    def _embed_batch(self, texts: List[str]) -> List[List[float]]:
        inputs = [self._truncate(t) for t in texts]
        last_exc = None
        for attempt in range(1, EMBED_MAX_ATTEMPTS + 1):
            future = self._watchdog.submit(self._raw_embed, inputs)
            try:
                response = future.result(timeout=EMBED_CALL_WATCHDOG_S)
                items = sorted(response.data, key=lambda d: d.index)
                return [item.embedding for item in items]
            except FuturesTimeout:
                # La chiamata e' appesa oltre il watchdog: abbandona il thread
                # (resta a morire da solo), ricrea il client e ritenta.
                last_exc = TimeoutError(
                    f"embedding batch appeso > {EMBED_CALL_WATCHDOG_S}s"
                )
                logger.warning(
                    "Embedding appeso (tentativo %s/%s): ricreo il client e ritento",
                    attempt, EMBED_MAX_ATTEMPTS,
                )
                if not self._injected:
                    self._client = self._new_client()
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "Embedding errore (tentativo %s/%s): %s",
                    attempt, EMBED_MAX_ATTEMPTS, str(exc)[:160],
                )
        raise RuntimeError(f"Embedding batch fallito dopo {EMBED_MAX_ATTEMPTS} tentativi: {last_exc}")

    def embed_documents(self, documents: Iterable[dict]) -> List[dict]:
        """Aggiunge `contentVector` ai documenti con content non vuoto.

        I documenti con content vuoto vengono restituiti senza vettore (e poi
        saltati a monte dall'orchestratore).
        """
        docs = list(documents)
        to_embed = [d for d in docs if d.get("content")]
        skipped = len(docs) - len(to_embed)
        if skipped:
            logger.info("Embedding: %s documenti senza content saltati", skipped)

        batch_size = self.config.batch_size
        for start in range(0, len(to_embed), batch_size):
            batch = to_embed[start : start + batch_size]
            vectors = self._embed_batch([d["content"] for d in batch])
            for doc, vector in zip(batch, vectors):
                doc["contentVector"] = vector
            logger.info(
                "Embedding: batch %s-%s completato (%s vettori)",
                start,
                start + len(batch),
                len(batch),
            )
        return docs
