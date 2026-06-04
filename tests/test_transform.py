"""Test sulla trasformazione record -> documento."""

from pipeline.transform import (
    build_content,
    build_header,
    build_incident_url,
    make_document_id,
    to_iso8601_z,
    transform_record,
    truncate_bytes,
)


def test_truncate_bytes_limits_size():
    text = "x" * 100000
    out = truncate_bytes(text, max_bytes=1000)
    assert len(out.encode("utf-8")) <= 1000


def test_truncate_bytes_keeps_short_text():
    assert truncate_bytes("breve") == "breve"


def test_truncate_bytes_no_broken_multibyte():
    # carattere multibyte (3 byte in UTF-8) ripetuto: il taglio non deve spezzarlo
    text = "à" * 1000  # 'à' = 2 byte
    out = truncate_bytes(text, max_bytes=101)  # taglio "scomodo"
    # decodifica valida (nessun carattere spezzato)
    assert out == out.encode("utf-8").decode("utf-8")


def _record():
    return {
        "number": {"value": "INC0012345", "display_value": "INC0012345"},
        "short_description": {"value": "Login fallito", "display_value": "Login fallito"},
        "description": {"value": "Utente non accede", "display_value": "Utente non accede"},
        "close_notes": {
            "value": "Reset con password=Segreta1",
            "display_value": "Reset con password=Segreta1",
        },
        "work_notes": {
            "value": "Connesso con scott/Tiger123@db:1521/ORCL e riavviato job",
            "display_value": "Connesso con scott/Tiger123@db:1521/ORCL e riavviato job",
        },
        "comments": {
            "value": "Gentile utente, risolto.",
            "display_value": "Gentile utente, risolto.",
        },
        "sys_id": {"value": "abc123sysid", "display_value": "abc123sysid"},
        "assignment_group": {"value": "grp-sysid-1", "display_value": "HD Oracle L2"},
        "priority": {"value": "4", "display_value": "4 - Low"},
        "impact": {"value": "3", "display_value": "3 - Low"},
        "urgency": {"value": "2", "display_value": "2 - Medium"},
        "closed_at": {"value": "2024-03-01 09:00:00", "display_value": "01/03/2024 10:00:00"},
        "sys_updated_on": {"value": "2024-03-01 09:05:00", "display_value": "..."},
    }


def test_make_document_id_cleans_invalid_chars():
    assert make_document_id("INC0012345") == "INC0012345"
    assert make_document_id("INC/00:12 345") == "INC_00_12_345"


def test_to_iso8601_z():
    assert to_iso8601_z("2024-03-01 09:00:00") == "2024-03-01T09:00:00Z"
    assert to_iso8601_z("") is None
    assert to_iso8601_z("not-a-date") is None


def test_build_content_concatenates():
    content = build_content("p", "d", "r", "wn", "cm")
    assert "Problema: p" in content
    assert "Descrizione: d" in content
    assert "Risoluzione: r" in content
    assert "Note di lavorazione: wn" in content
    assert "Commenti: cm" in content


def test_build_content_omits_empty_journal():
    content = build_content("p", "d", "r")
    assert "Note di lavorazione" not in content
    assert "Commenti" not in content


def test_build_header():
    h = build_header(
        "INC0012345",
        "HD Oracle L2",
        "2024-03-01T09:00:00Z",
        "https://acme.service-now.com/nav_to.do?uri=incident.do?sys_id=x",
    )
    assert "Ticket INC0012345" in h
    assert "Gruppo: HD Oracle L2" in h
    assert "Chiuso: 2024-03-01" in h
    assert "Link: https://acme.service-now.com" in h
    # niente parte oraria nella data
    assert "09:00:00" not in h


def test_build_incident_url():
    base = "https://acme.service-now.com"
    url = build_incident_url(base, "abc123sysid", "INC0012345")
    assert url == "https://acme.service-now.com/nav_to.do?uri=incident.do?sys_id=abc123sysid"
    # fallback per numero se manca il sys_id
    url2 = build_incident_url(base, "", "INC0012345")
    assert "number=INC0012345" in url2
    # senza sys_id ne' number -> stringa vuota
    assert build_incident_url(base, "", "") == ""


def test_content_starts_with_ticket_header():
    doc = transform_record(_record())
    # il content deve aprire con l'header che cita il numero ticket
    assert doc["content"].startswith("Ticket INC0012345")
    assert "Gruppo: HD Oracle L2" in doc["content"]


def test_transform_record_includes_url_with_base():
    doc = transform_record(_record(), base_url="https://acme.service-now.com")
    assert doc["url"].endswith("sys_id=abc123sysid")
    assert "Link: https://acme.service-now.com" in doc["content"]


def test_transform_record_url_empty_without_base():
    doc = transform_record(_record())
    assert doc["url"] == ""
    assert "Link:" not in doc["content"]


def test_transform_record_maps_fields_and_redacts():
    doc = transform_record(_record())
    assert doc["id"] == "INC0012345"
    assert doc["number"] == "INC0012345"
    # cmdb_ci non e' piu' indicizzato (sempre vuoto su Amplifon)
    assert "cmdb_ci" not in doc
    assert "cmdb_ci_name" not in doc
    assert doc["assignment_group_name"] == "HD Oracle L2"
    assert doc["closed_at"] == "2024-03-01T09:00:00Z"
    # metadati di severita'
    assert doc["priority"] == "4 - Low"
    assert doc["impact"] == "3 - Low"
    assert doc["urgency"] == "2 - Medium"
    # redaction applicata nelle close_notes -> resolution e content
    assert "Segreta1" not in doc["resolution"]
    assert "Segreta1" not in doc["content"]


def test_transform_record_includes_and_redacts_journal_fields():
    doc = transform_record(_record())
    # work_notes e comments presenti e confluiti nel content
    assert "Note di lavorazione" in doc["content"]
    assert "Commenti" in doc["content"]
    # la password nella connection string dentro work_notes e' mascherata
    assert "Tiger123" not in doc["work_notes"]
    assert "Tiger123" not in doc["content"]
    # utente/host preservati per contesto
    assert "scott" in doc["work_notes"]
