"""Interroga l'indice Azure AI Search per verifica/diagnostica.

    python -m pipeline.query_index --count
    python -m pipeline.query_index --search "errore JDE" --top 3
    python -m pipeline.query_index --doc INC0011642
"""

from __future__ import annotations

import argparse
import json
import sys

from .config import SearchConfig


def _client(search: SearchConfig):
    from azure.search.documents import SearchClient

    if search.use_aad:
        from azure.identity import DefaultAzureCredential

        cred = DefaultAzureCredential()
    else:
        from azure.core.credentials import AzureKeyCredential

        cred = AzureKeyCredential(search.admin_key)
    return SearchClient(search.endpoint, search.index_name, cred)


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="pipeline.query_index")
    p.add_argument("--count", action="store_true", help="Conta i documenti nell'indice.")
    p.add_argument("--search", help="Testo da cercare (full-text).")
    p.add_argument("--doc", help="Recupera un documento per id (es. INC0011642).")
    p.add_argument("--top", type=int, default=5, help="Numero risultati (default 5).")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    search = SearchConfig.from_env()
    client = _client(search)

    if args.count or (not args.search and not args.doc):
        total = client.get_document_count()
        print(f"Documenti nell'indice '{search.index_name}': {total}")

    if args.doc:
        doc = client.get_document(key=args.doc)
        # get_document ritorna un LookupDocument (dict-like): convertiamo a dict.
        print(json.dumps(dict(doc), ensure_ascii=False, indent=2))

    if args.search:
        results = client.search(search_text=args.search, top=args.top)
        print(f"\nRisultati per '{args.search}':")
        for r in results:
            score = r.get("@search.score")
            print(
                f"  - {r.get('number')} | {r.get('short_description')!r} "
                f"| group={r.get('assignment_group_name')!r} | score={score:.3f}"
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
