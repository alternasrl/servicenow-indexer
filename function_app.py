"""Azure Functions (modello di programmazione Python v2).

- Timer trigger: corsa notturna delle 02:00 (delta).
- HTTP trigger: backfill manuale / full load on demand.

Il default operativo per ora resta l'esecuzione locale (vedi pipeline/run.py);
questi trigger rendono immediato il passaggio in cloud condividendo la stessa
logica di orchestrazione.
"""

from __future__ import annotations

import json
import logging

import azure.functions as func

from pipeline.config import AppConfig
from pipeline.orchestrator import Pipeline

app = func.FunctionApp()


@app.function_name(name="nightly_delta")
@app.timer_trigger(
    schedule="0 0 2 * * *",  # ogni giorno alle 02:00
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def nightly_delta(timer: func.TimerRequest) -> None:
    logging.info("Timer trigger: avvio run delta notturno")
    config = AppConfig.from_env()
    stats = Pipeline(config=config).run()
    logging.info("Run delta completato: %s", stats.as_dict())


@app.function_name(name="manual_backfill")
@app.route(route="backfill", auth_level=func.AuthLevel.FUNCTION)
def manual_backfill(req: func.HttpRequest) -> func.HttpResponse:
    """Backfill / full load on demand.

    Query params:
      ?full=true                            -> full load
      ?backfill_from=YYYY-MM-DD HH:MM:SS    -> backfill da data
    """
    full = (req.params.get("full") or "").lower() == "true"
    backfill_from = req.params.get("backfill_from")

    logging.info(
        "HTTP trigger: backfill (full=%s, backfill_from=%s)", full, backfill_from
    )
    config = AppConfig.from_env()
    stats = Pipeline(config=config).run(full=full, backfill_from=backfill_from)

    return func.HttpResponse(
        body=json.dumps(stats.as_dict(), ensure_ascii=False),
        mimetype="application/json",
        status_code=200,
    )
