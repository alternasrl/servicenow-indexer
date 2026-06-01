"""Entry point locale.

Esempi:
    python -m pipeline.run                       # delta (o full al primo run)
    python -m pipeline.run --full                # forza il full load
    python -m pipeline.run --backfill-from "2024-01-01 00:00:00"
"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import AppConfig, ConfigError
from .orchestrator import Pipeline


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pipeline.run",
        description="Ingestion ServiceNow -> Azure AI Search",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Forza il full load ignorando il watermark.",
    )
    parser.add_argument(
        "--backfill-from",
        metavar="DATETIME",
        help="Backfill manuale da una data ServiceNow GMT 'YYYY-MM-DD HH:MM:SS'. "
        "Non aggiorna il watermark.",
    )
    parser.add_argument(
        "--max-records",
        type=int,
        metavar="N",
        help="Limita l'estrazione a N record (smoke test, non per la produzione).",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Log DEBUG.")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    _setup_logging(args.verbose)
    log = logging.getLogger("pipeline.run")

    try:
        config = AppConfig.from_env()
    except ConfigError as exc:
        log.error("Configurazione non valida: %s", exc)
        return 2

    pipeline = Pipeline(config=config)
    stats = pipeline.run(
        full=args.full,
        backfill_from=args.backfill_from,
        max_records=args.max_records,
    )

    log.info("==== STATISTICHE RUN ====")
    for key, value in stats.as_dict().items():
        log.info("%-20s %s", key, value)
    return 0


if __name__ == "__main__":
    sys.exit(main())
