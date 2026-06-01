"""Ispeziona un singolo incident con un set di campi ESTESO.

Serve a decidere cosa vale la pena indicizzare: mostra, per ogni campo, la
lunghezza del valore e un'anteprima, includendo i journal field (comments,
work_notes) che spesso contengono la vera conoscenza risolutiva.

Esempi:
    python -m pipeline.inspect_record                 # primo incident chiuso
    python -m pipeline.inspect_record --number INC0012345
"""

from __future__ import annotations

import argparse
import sys

from .config import ServiceNowConfig
from .servicenow import build_query, ServiceNowClient

# Set esteso (NON quello di produzione): solo per ispezione.
EXTENDED_FIELDS = [
    "number",
    "short_description",
    "description",
    "close_notes",
    "comments",          # journal: additional comments (cliente)
    "work_notes",        # journal: note interne tecniche
    "comments_and_work_notes",
    "category",
    "subcategory",
    "u_category",
    "cmdb_ci",
    "business_service",
    "service_offering",
    "assignment_group",
    "assigned_to",
    "priority",
    "severity",
    "urgency",
    "impact",
    "contact_type",
    "resolved_at",
    "closed_at",
    "opened_at",
    "sys_created_on",
    "sys_updated_on",
    "resolved_by",
    "closed_by",
    "caller_id",
    "company",
    "location",
]


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="pipeline.inspect_record")
    p.add_argument("--number", help="Numero ticket specifico (es. INC0012345).")
    return p.parse_args(argv)


def _cell(raw):
    """Ritorna (value, display_value) qualunque sia la forma del campo."""
    if isinstance(raw, dict):
        return raw.get("value", ""), raw.get("display_value", "")
    return raw, raw


def main(argv=None) -> int:
    args = parse_args(argv)
    config = ServiceNowConfig.from_env(require_filters=False)
    client = ServiceNowClient(config)

    if args.number:
        query = f"number={args.number}"
    else:
        query = build_query(config)  # primo incident chiuso

    record = None
    for rec in client.iter_records(
        fields=EXTENDED_FIELDS, max_records=1, query_override=query
    ):
        record = rec
        break

    if record is None:
        print("Nessun record trovato.")
        return 1

    num = _cell(record.get("number"))[0]
    print(f"\n=== INCIDENT {num} — campi disponibili ===\n")
    print(f"{'CAMPO':<26} {'LEN':>6}  ANTEPRIMA (display_value)")
    print("-" * 100)
    for field in EXTENDED_FIELDS:
        if field not in record:
            print(f"{field:<26} {'--':>6}  (non presente / non leggibile)")
            continue
        value, display = _cell(record[field])
        text = (display or value or "").replace("\r", " ").replace("\n", " ")
        length = len(text)
        preview = text[:80] + (" […]" if length > 80 else "")
        print(f"{field:<26} {length:>6}  {preview}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
