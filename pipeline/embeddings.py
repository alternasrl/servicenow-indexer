"""Embedding del campo `content` via Azure OpenAI.

- Lavora a batch.
- Salta i documenti con content vuoto (non genera vettore).
- Usa lo stesso modello/deployment che verra' configurato come vectorizer
  nell'indice Azure AI Search, cosi' query e documenti vivono nello stesso
  spazio vettoriale.
"""

from __future__ import annotations

import logging
from typing import Iterable, List

from .config import EmbeddingConfig

logger = logging.getLogger(__name__)


class EmbeddingClient:
    def __init__(self, config: EmbeddingConfig, client=None) -> None:
        self.config = config
        if client is not None:
            self._client = client
        else:
            from openai import AzureOpenAI  # import lazy

            self._client = AzureOpenAI(
                azure_endpoint=config.endpoint,
                api_key=config.api_key,
                api_version=config.api_version,
            )

    def _embed_batch(self, texts: List[str]) -> List[List[float]]:
        response = self._client.embeddings.create(
            model=self.config.deployment,
            input=texts,
            dimensions=self.config.dimensions,
        )
        # L'API garantisce l'ordine, ma ordiniamo per index per sicurezza.
        items = sorted(response.data, key=lambda d: d.index)
        return [item.embedding for item in items]

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
