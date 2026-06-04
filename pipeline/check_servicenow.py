"""Diagnostica dell'estrazione ServiceNow IN ISOLAMENTO.

Verifica passo-passo solo la parte ServiceNow, senza dipendere dalla
configurazione di Azure AI Search o Azure OpenAI:

  1. carica la sola ServiceNowConfig dalle variabili d'ambiente;
  2. stampa la query/filtro che verra' usata;
  3. ping (connettivita' + autenticazione);
  4. count via Aggregate API (quanti ticket rispettano il filtro);
  5. fetch di un piccolo campione e mapping a documento (con redaction);
  6. stampa un'anteprima leggibile del primo documento.

Esempi:
    python -m pipeline.check_servicenow                 # campione 5 record
    python -m pipeline.check_servicenow --sample 20
    python -m pipeline.check_servicenow --no-filters    # ignora la guardia gruppi/CI
    python -m pipeline.check_servicenow --raw           # mostra anche il record grezzo
    python -m pipeline.check_servicenow --backfill-from "2024-01-01 00:00:00"
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Optional

import requests

from .config import ConfigError, ServiceNowConfig
from .servicenow import DEFAULT_FIELDS, ServiceNowClient, build_query
from .transform import record_updated_on, transform_record


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pipeline.check_servicenow",
        description="Diagnostica dell'estrazione ServiceNow in isolamento.",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=5,
        help="Numero massimo di record da scaricare per l'anteprima (default 5).",
    )
    parser.add_argument(
        "--require-filters",
        action="store_true",
        help="Pretende che resolver group e CI siano valorizzati (errore se vuoti). "
        "Di default i filtri sono opzionali: se vuoti si estrae tutto.",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Stampa anche il record grezzo restituito da ServiceNow.",
    )
    parser.add_argument(
        "--backfill-from",
        metavar="DATETIME",
        help="Applica un watermark 'YYYY-MM-DD HH:MM:SS' (GMT) per provare il delta.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Log DEBUG.")
    return parser.parse_args(argv)


def _print_header(title: str) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def main(argv=None) -> int:
    args = parse_args(argv)
    _setup_logging(args.verbose)
    log = logging.getLogger("check_servicenow")

    # 1) Config (sola parte ServiceNow).
    try:
        config = ServiceNowConfig.from_env(require_filters=args.require_filters)
    except ConfigError as exc:
        log.error("Configurazione ServiceNow non valida: %s", exc)
        return 2

    _print_header("1) CONFIGURAZIONE")
    print(f"  base_url            : {config.base_url}")
    print(f"  table               : {config.table}")
    print(f"  user                : {config.user}")
    print(f"  closed_states       : {config.closed_states}")
    print(f"  resolver_groups     : {config.resolver_groups or '(vuoto)'}")
    print(f"  configuration_items : {config.configuration_items or '(vuoto)'}")
    print(f"  page_size           : {config.page_size}")
    print(f"  campi richiesti     : {', '.join(DEFAULT_FIELDS)}")

    watermark: Optional[str] = args.backfill_from

    _print_header("2) QUERY / FILTRO (sysparm_query)")
    print(f"  {build_query(config, watermark)}")
    if watermark:
        print(f"  (watermark applicato: {watermark})")

    client = ServiceNowClient(config)

    # 3) Ping.
    _print_header("3) PING (connettivita' + autenticazione)")
    try:
        status = client.ping()
        print(f"  OK - HTTP {status}")
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else "?"
        if code == 401:
            log.error("Autenticazione fallita (401): controlla utente/password.")
        else:
            log.error("Errore HTTP %s durante il ping: %s", code, exc)
        return 1
    except requests.RequestException as exc:
        log.error("Istanza non raggiungibile: %s", exc)
        return 1

    # 4) Count.
    _print_header("4) COUNT (Aggregate API, nessun download)")
    try:
        total = client.count(watermark)
        print(f"  Ticket che rispettano il filtro: {total}")
    except requests.RequestException as exc:
        log.warning(
            "Count non disponibile (l'utente potrebbe non avere accesso alla "
            "Aggregate API): %s",
            exc,
        )
        total = None

    if total == 0:
        print(
            "\n  ATTENZIONE: 0 ticket. Verifica sys_id di gruppi/CI, stati chiusi "
            "e che esistano incident chiusi per quei criteri."
        )

    # 5) Fetch campione + mapping.
    _print_header(f"5) CAMPIONE ({args.sample} record) + MAPPING")
    count = 0
    first_doc = None
    first_raw = None
    max_updated = None
    for record in client.iter_records(watermark=watermark, max_records=args.sample):
        count += 1
        doc = transform_record(record, base_url=config.base_url)
        max_updated = record_updated_on(record) or max_updated
        if first_doc is None:
            first_doc = doc
            first_raw = record
        print(
            f"  - {doc['number']:<14} "
            f"| group={doc['assignment_group_name'] or doc['assignment_group']!r} "
            f"| prio={doc['priority']!r} "
            f"| closed_at={doc['closed_at']}"
        )

    if count == 0:
        print("  (nessun record restituito)")
        return 0

    print(f"\n  Record scaricati nel campione : {count}")
    print(f"  max sys_updated_on nel campione: {max_updated}")

    # 6) Anteprima primo documento.
    _print_header("6) ANTEPRIMA PRIMO DOCUMENTO (post-redaction)")
    preview = dict(first_doc)
    # content puo' essere lungo: lo tronchiamo per leggibilita'.
    if preview.get("content") and len(preview["content"]) > 600:
        preview["content"] = preview["content"][:600] + " […]"
    print(json.dumps(preview, ensure_ascii=False, indent=2))

    if args.raw:
        _print_header("RECORD GREZZO (display_value=all)")
        print(json.dumps(first_raw, ensure_ascii=False, indent=2))

    print("\nOK: la parte ServiceNow funziona.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
