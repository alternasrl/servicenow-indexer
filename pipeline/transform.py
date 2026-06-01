"""Trasformazione record ServiceNow -> documento Azure AI Search.

- Un documento per ticket.
- Con `sysparm_display_value=all` ogni campo arriva come
  {"value": "...", "display_value": "..."}; per i reference (assignment_group)
  `value` = sys_id, `display_value` = nome leggibile.
- La redaction viene applicata a tutti i campi testuali liberi.
- `content` concatena problema + descrizione + risoluzione + journal field
  (gia' rediretti).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from .redaction import Redactor, default_redactor

logger = logging.getLogger(__name__)

# Caratteri ammessi nella chiave di Azure AI Search: lettere, cifre, _, -, =.
_KEY_INVALID = re.compile(r"[^A-Za-z0-9_\-=]")


def make_document_id(number: str) -> str:
    """Deriva l'id documento dal numero ticket ripulendo i caratteri non ammessi."""
    return _KEY_INVALID.sub("_", number.strip())


def _value(record: dict, field: str) -> str:
    raw = record.get(field)
    if isinstance(raw, dict):
        return (raw.get("value") or "").strip()
    return (raw or "").strip() if isinstance(raw, str) else ""


def _display(record: dict, field: str) -> str:
    raw = record.get(field)
    if isinstance(raw, dict):
        return (raw.get("display_value") or "").strip()
    return (raw or "").strip() if isinstance(raw, str) else ""


def to_iso8601_z(sn_datetime: str) -> Optional[str]:
    """Converte 'YYYY-MM-DD HH:mm:ss' (GMT) in ISO 8601 con suffisso Z."""
    if not sn_datetime:
        return None
    try:
        dt = datetime.strptime(sn_datetime, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        logger.warning("Data non parsabile, ignorata: %r", sn_datetime)
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_content(
    short_description: str,
    description: str,
    resolution: str,
    work_notes: str = "",
    comments: str = "",
) -> str:
    """Concatena i campi testuali nel contenuto indicizzato.

    Oltre a problema/descrizione/risoluzione include le note interne tecniche
    (work_notes) e i commenti col cliente (comments), che spesso contengono la
    vera conoscenza risolutiva. Tutti i campi devono arrivare gia' rediretti.
    """
    parts = []
    if short_description:
        parts.append(f"Problema: {short_description}")
    if description:
        parts.append(f"Descrizione: {description}")
    if resolution:
        parts.append(f"Risoluzione: {resolution}")
    if work_notes:
        parts.append(f"Note di lavorazione: {work_notes}")
    if comments:
        parts.append(f"Commenti: {comments}")
    return "\n\n".join(parts).strip()


def transform_record(record: dict, redactor: Optional[Redactor] = None) -> dict:
    """Trasforma un record ServiceNow in un documento per l'indice."""
    redactor = redactor or default_redactor

    number = _value(record, "number") or _display(record, "number")
    short_description = redactor.redact(_display(record, "short_description"))
    description = redactor.redact(_display(record, "description"))
    resolution = redactor.redact(_display(record, "close_notes"))
    # Journal field: redatti come tutto il resto (contengono spesso credenziali).
    work_notes = redactor.redact(_display(record, "work_notes"))
    comments = redactor.redact(_display(record, "comments"))

    content = build_content(
        short_description, description, resolution, work_notes, comments
    )

    doc = {
        "id": make_document_id(number),
        "number": number,
        "short_description": short_description,
        "description": description,
        "resolution": resolution,
        "work_notes": work_notes,
        "comments": comments,
        "content": content,
        # NB: cmdb_ci su Amplifon e' sistematicamente vuoto -> non indicizzato.
        "assignment_group": _value(record, "assignment_group"),
        "assignment_group_name": _display(record, "assignment_group"),
        # Metadati di severita' (display_value leggibile, es. "4 - Low").
        "priority": _display(record, "priority"),
        "impact": _display(record, "impact"),
        "urgency": _display(record, "urgency"),
        "closed_at": to_iso8601_z(_value(record, "closed_at")),
    }
    return doc


def record_updated_on(record: dict) -> str:
    """Estrae sys_updated_on (value raw = GMT) per l'avanzamento del watermark."""
    return _value(record, "sys_updated_on")
