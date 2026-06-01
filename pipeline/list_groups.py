"""Utility: elenca i resolver group (sys_user_group) per ricavarne i sys_id.

Cerca i gruppi il cui nome contiene uno dei termini passati (default: Oracle,
JDE) e ne stampa sys_id + nome, da copiare in SERVICENOW_RESOLVER_GROUPS.

Riusa l'autenticazione (OAuth/basic) gia' configurata in .env.

Esempi:
    python -m pipeline.list_groups
    python -m pipeline.list_groups --contains Oracle,JDE,Financials
    python -m pipeline.list_groups --all
"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import ServiceNowConfig
from .servicenow import ServiceNowClient


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="pipeline.list_groups")
    parser.add_argument(
        "--contains",
        default="Oracle,JDE",
        help="Termini (CSV) da cercare nel nome gruppo (default: Oracle,JDE).",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Elenca tutti i gruppi attivi (ignora --contains).",
    )
    parser.add_argument(
        "--limit", type=int, default=200, help="Max gruppi da scaricare (default 200)."
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    # Config ServiceNow, ma puntando alla tabella dei gruppi.
    config = ServiceNowConfig.from_env(require_filters=False)
    config.table = "sys_user_group"

    client = ServiceNowClient(config)

    if args.all:
        query = "active=true^ORDERBYname"
    else:
        terms = [t.strip() for t in args.contains.split(",") if t.strip()]
        # name LIKE term1 OR name LIKE term2 ...
        like = "^OR".join(f"nameLIKE{t}" for t in terms)
        query = f"active=true^{like}^ORDERBYname"

    fields = ["sys_id", "name", "description"]
    print(f"\nQuery gruppi: {query}\n")
    print(f"{'SYS_ID':<34} | NOME")
    print("-" * 80)

    rows = []
    for rec in client.iter_records(
        fields=fields, max_records=args.limit, query_override=query
    ):
        # con display_value=all i campi sono {value, display_value}
        sys_id = rec.get("sys_id", {})
        name = rec.get("name", {})
        sys_id = sys_id.get("value") if isinstance(sys_id, dict) else sys_id
        name = name.get("display_value") if isinstance(name, dict) else name
        rows.append((sys_id, name))
        print(f"{sys_id:<34} | {name}")

    print(f"\nTotale: {len(rows)} gruppi")
    if rows and not args.all:
        csv = ",".join(r[0] for r in rows)
        print("\nPer includerli tutti nel filtro:")
        print(f"SERVICENOW_RESOLVER_GROUPS={csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
