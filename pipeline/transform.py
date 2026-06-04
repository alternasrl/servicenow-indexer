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

# Limite Azure AI Search: un termine UTF-8 non puo' superare 32766 byte.
# Teniamo un margine prudente per i campi testuali lunghi (journal, content).
MAX_FIELD_BYTES = 30000


def truncate_bytes(text: str, max_bytes: int = MAX_FIELD_BYTES) -> str:
    """Tronca una stringa in modo che non superi `max_bytes` in UTF-8.

    Evita l'errore di Azure AI Search sui campi con termini troppo grandi e non
    spezza i caratteri multibyte. I journal restano comunque interi nel testo
    originale ServiceNow; qui si limita solo il valore indicizzato.
    """
    if not text:
        return text
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


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


def build_incident_url(base_url: str, sys_id: str, number: str = "") -> str:
    """URL diretto all'incident in ServiceNow.

    Forma standard: <base>/nav_to.do?uri=incident.do?sys_id=<sys_id>
    Se manca il sys_id ma c'e' il numero, usa la ricerca per numero.
    """
    base = base_url.rstrip("/")
    if sys_id:
        return f"{base}/nav_to.do?uri=incident.do?sys_id={sys_id}"
    if number:
        return f"{base}/incident.do?sysparm_query=number={number}"
    return ""


def build_header(
    number: str,
    assignment_group_name: str = "",
    closed_at_iso: str = "",
    url: str = "",
) -> str:
    """Header di citazione anteposto al content.

    Serve perche' Copilot Studio non espone il mapping dei campi: passa al
    modello solo title/content. Inserendo numero ticket, gruppo, data e LINK
    DENTRO il content, il modello puo' sempre citare il ticket e riportare l'URL.
    """
    bits = [f"Ticket {number}"] if number else []
    if assignment_group_name:
        bits.append(f"Gruppo: {assignment_group_name}")
    if closed_at_iso:
        # Solo la data (YYYY-MM-DD) per leggibilita'.
        bits.append(f"Chiuso: {closed_at_iso[:10]}")
    if url:
        bits.append(f"Link: {url}")
    return " | ".join(bits)


def build_content(
    short_description: str,
    description: str,
    resolution: str,
    work_notes: str = "",
    comments: str = "",
    header: str = "",
) -> str:
    """Concatena i campi testuali nel contenuto indicizzato.

    Oltre a problema/descrizione/risoluzione include le note interne tecniche
    (work_notes) e i commenti col cliente (comments), che spesso contengono la
    vera conoscenza risolutiva. Tutti i campi devono arrivare gia' rediretti.
    Un eventuale `header` (numero ticket + metadati) viene anteposto.
    """
    parts = []
    if header:
        parts.append(header)
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


def transform_record(
    record: dict,
    redactor: Optional[Redactor] = None,
    base_url: str = "",
) -> dict:
    """Trasforma un record ServiceNow in un documento per l'indice.

    `base_url` (es. https://acme.service-now.com) serve a costruire il link
    diretto all'incident, inserito nell'header del content e nel campo `url`.
    """
    redactor = redactor or default_redactor

    number = _value(record, "number") or _display(record, "number")
    sys_id = _value(record, "sys_id")
    short_description = redactor.redact(_display(record, "short_description"))
    description = redactor.redact(_display(record, "description"))
    resolution = redactor.redact(_display(record, "close_notes"))
    # Journal field: redatti come tutto il resto (contengono spesso credenziali).
    work_notes = redactor.redact(_display(record, "work_notes"))
    comments = redactor.redact(_display(record, "comments"))

    assignment_group_name = _display(record, "assignment_group")
    closed_at = to_iso8601_z(_value(record, "closed_at"))
    url = build_incident_url(base_url, sys_id, number) if base_url else ""

    # Header di citazione (numero ticket + gruppo + data + link) dentro il
    # content: Copilot Studio passa al modello solo title/content, quindi mettiamo
    # qui le info per la citazione e l'URL dell'incident.
    header = build_header(number, assignment_group_name, closed_at, url)

    content = build_content(
        short_description, description, resolution, work_notes, comments, header
    )

    doc = {
        "id": make_document_id(number),
        "number": number,
        "url": url,
        "short_description": short_description,
        "description": truncate_bytes(description),
        "resolution": truncate_bytes(resolution),
        "work_notes": truncate_bytes(work_notes),
        "comments": truncate_bytes(comments),
        "content": truncate_bytes(content),
        # NB: cmdb_ci su Amplifon e' sistematicamente vuoto -> non indicizzato.
        "assignment_group": _value(record, "assignment_group"),
        "assignment_group_name": assignment_group_name,
        # Metadati di severita' (display_value leggibile, es. "4 - Low").
        "priority": _display(record, "priority"),
        "impact": _display(record, "impact"),
        "urgency": _display(record, "urgency"),
        "closed_at": closed_at,
    }
    return doc


def record_updated_on(record: dict) -> str:
    """Estrae sys_updated_on (value raw = GMT) per l'avanzamento del watermark."""
    return _value(record, "sys_updated_on")
