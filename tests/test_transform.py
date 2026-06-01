"""Test sulla trasformazione record -> documento."""

from pipeline.transform import (
    build_content,
    make_document_id,
    to_iso8601_z,
    transform_record,
)


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
