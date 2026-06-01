"""Diagnostica dell'embedding Azure OpenAI / Foundry in isolamento.

Verifica che il deployment di embedding risponda e che il vettore abbia il
numero di dimensioni atteso, senza toccare ServiceNow o Azure AI Search.

    python -m pipeline.check_embeddings
    python -m pipeline.check_embeddings --text "errore JDE F5801SXR"
"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import EmbeddingConfig, ConfigError
from .embeddings import EmbeddingClient


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="pipeline.check_embeddings")
    p.add_argument(
        "--text",
        default="Test di embedding: errore Oracle JDE su tabella F5801SXR.",
        help="Testo di prova da vettorializzare.",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    log = logging.getLogger("check_embeddings")

    try:
        cfg = EmbeddingConfig.from_env()
    except ConfigError as exc:
        log.error("Configurazione embedding non valida: %s", exc)
        return 2

    print("\n=== CONFIG EMBEDDING ===")
    print(f"  endpoint    : {cfg.endpoint}")
    print(f"  deployment  : {cfg.deployment}")
    print(f"  model       : {cfg.model}")
    print(f"  dimensions  : {cfg.dimensions}")
    print(f"  api_version : {cfg.api_version}")

    client = EmbeddingClient(cfg)
    docs = [{"content": args.text}]
    out = client.embed_documents(docs)

    vec = out[0].get("contentVector")
    if not vec:
        print("\nERRORE: nessun vettore restituito.")
        return 1

    print("\n=== RISULTATO ===")
    print(f"  dimensioni vettore : {len(vec)} (attese {cfg.dimensions})")
    print(f"  primi 5 valori     : {vec[:5]}")
    if len(vec) != cfg.dimensions:
        print("  ATTENZIONE: dimensioni diverse da quelle configurate!")
        return 1
    print("\nOK: l'embedding funziona.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
