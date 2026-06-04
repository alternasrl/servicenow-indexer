"""Diagnostica Azure AI Search in isolamento.

Verifica la connessione (con admin key o Azure AD), crea l'indice se non
esiste e ne stampa i campi principali. Non tocca ServiceNow.

    python -m pipeline.check_search                 # verifica + crea indice
    python -m pipeline.check_search --dump-schema   # stampa solo lo schema JSON
    python -m pipeline.check_search --recreate      # elimina e ricrea l'indice
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

import requests

from .config import ConfigError, EmbeddingConfig, SearchConfig
from .search_index import (
    SEARCH_AAD_SCOPE,
    SearchIndexManager,
    build_index_definition,
)


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="pipeline.check_search")
    p.add_argument(
        "--dump-schema",
        action="store_true",
        help="Stampa lo schema JSON dell'indice senza contattare il servizio.",
    )
    p.add_argument(
        "--recreate",
        action="store_true",
        help="Elimina l'indice esistente e lo ricrea (ATTENZIONE: perde i dati).",
    )
    p.add_argument(
        "--update-schema",
        action="store_true",
        help="Aggiorna lo schema dell'indice esistente (aggiunge nuovi campi, "
        "es. 'url') senza perdere i dati.",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    log = logging.getLogger("check_search")

    try:
        search = SearchConfig.from_env()
        embedding = EmbeddingConfig.from_env()
    except ConfigError as exc:
        log.error("Configurazione non valida: %s", exc)
        return 2

    if args.dump_schema:
        definition = build_index_definition(search.index_name, embedding)
        print(json.dumps(definition, ensure_ascii=False, indent=2))
        return 0

    print("\n=== CONFIG SEARCH ===")
    print(f"  endpoint   : {search.endpoint}")
    print(f"  index      : {search.index_name}")
    print(f"  auth       : {'Azure AD (RBAC)' if search.use_aad else 'admin key'}")
    print(f"  api_version: {search.api_version}")

    manager = SearchIndexManager(search, embedding)

    # 1) Verifica autenticazione / connettivita'.
    print("\n=== AUTENTICAZIONE / CONNETTIVITA' ===")
    try:
        if search.use_aad:
            token = manager._credential.get_token(SEARCH_AAD_SCOPE)
            print("  Token Azure AD ottenuto (scope search.azure.com). OK")
        exists = manager.index_exists()
        print(f"  Connessione al servizio OK. Indice esiste: {exists}")
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else "?"
        if code in (401, 403):
            log.error(
                "Accesso negato (HTTP %s). Verifica i ruoli RBAC: servono "
                "'Search Service Contributor' (creazione indice) e "
                "'Search Index Data Contributor' (scrittura). E fai 'az login'.",
                code,
            )
        else:
            log.error("Errore HTTP %s: %s", code, exc)
        return 1
    except Exception as exc:  # es. credenziale AAD assente
        log.error("Errore di autenticazione: %s", exc)
        log.error("Suggerimento: esegui 'az login' su questa macchina.")
        return 1

    # 2) (Ri)creazione indice.
    if args.recreate and exists:
        print("\n=== RICREAZIONE INDICE ===")
        resp = requests.delete(
            manager._index_url(), headers=manager._build_headers(), timeout=60
        )
        if resp.status_code not in (200, 204):
            log.error("Delete fallito: %s %s", resp.status_code, resp.text)
            return 1
        print("  Indice eliminato.")
        exists = False

    if args.update_schema and exists:
        print("\n=== UPDATE SCHEMA ===")
        manager.update_index()
    else:
        print("\n=== ENSURE INDEX ===")
        manager.ensure_index()

    # 3) Riepilogo campi.
    definition = build_index_definition(search.index_name, embedding)
    print("\n=== CAMPI INDICE ===")
    for f in definition["fields"]:
        flags = []
        for k in ("key", "searchable", "filterable", "facetable", "sortable"):
            if f.get(k):
                flags.append(k)
        if f.get("retrievable") is False:
            flags.append("NOT-retrievable")
        print(f"  {f['name']:<24} {f['type']:<28} {', '.join(flags)}")

    print("\nOK: indice pronto su Azure AI Search.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
