"""Test unitari sulla redaction."""

from pipeline.redaction import MASK, Redactor, redact


def test_password_assignment_equals():
    out = redact("login con password=Segreta123 ok")
    assert "Segreta123" not in out
    assert MASK in out


def test_pwd_colon():
    out = redact("pwd: hunter2")
    assert "hunter2" not in out
    assert MASK in out


def test_oracle_identified_by():
    out = redact("CREATE USER hr IDENTIFIED BY MyOraclePwd;")
    assert "MyOraclePwd" not in out
    assert MASK in out
    # l'utente resta visibile
    assert "hr" in out


def test_oracle_identified_by_values():
    out = redact("ALTER USER scott IDENTIFIED BY VALUES 'S:abc123def';")
    assert "abc123def" not in out
    assert MASK in out


def test_oracle_connection_string_masks_only_password():
    out = redact("connessione system/Passw0rd@db-host:1521/ORCLPDB")
    assert "Passw0rd" not in out
    assert MASK in out
    # utente, host e servizio restano per contesto
    assert "system" in out
    assert "db-host:1521/ORCLPDB" in out


def test_empty_and_none():
    assert redact("") == ""
    assert redact(None) == ""


def test_no_false_positive_on_plain_text():
    text = "Il ticket riguarda un problema di performance sul DB."
    assert redact(text) == text


def test_custom_rules_extension_point():
    import re

    from pipeline.redaction import RedactionRule

    rule = RedactionRule(
        name="email",
        pattern=re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
        replacement=MASK,
    )
    redactor = Redactor(rules=[rule])
    out = redactor.redact("scrivi a mario.rossi@example.com per dettagli")
    assert "mario.rossi@example.com" not in out
    assert MASK in out
