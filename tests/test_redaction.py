"""Test unitari sulla redaction.

NB: usiamo un Redactor REGEX-ONLY esplicito (senza PII), non il redact() globale
del modulo: quest'ultimo puo' agganciare il backend PII (LLM/Presidio) in base
all'ambiente, rendendo i test non deterministici e dipendenti da servizi esterni.
"""

from pipeline.redaction import MASK, Redactor

# Redactor con le sole regole regex di default (nessun backend PII).
_rx = Redactor()
redact = _rx.redact


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


# --- Integrazione PII (Presidio) con redactor fittizio ---

class _FakePii:
    """Finto PiiRedactor: maschera un nome noto, registra la lingua ricevuta."""

    def __init__(self):
        self.calls = []

    def redact(self, text, language="it"):
        self.calls.append(language)
        return text.replace("Andrea Di Cosmo", "[PII]")


def test_redactor_applies_pii_after_regex():
    fake = _FakePii()
    redactor = Redactor(pii_redactor=fake)
    out = redactor.redact("Ticket gestito da Andrea Di Cosmo con password=Segreta1")
    # regex maschera la password
    assert "Segreta1" not in out
    # PII maschera il nome
    assert "Andrea Di Cosmo" not in out
    assert "[PII]" in out


def test_redactor_passes_language_to_pii():
    fake = _FakePii()
    redactor = Redactor(pii_redactor=fake)
    redactor.redact("testo", language="en")
    assert fake.calls == ["en"]


def test_redactor_without_pii_is_regex_only():
    # nessun pii_redactor -> comportamento invariato (solo regex)
    redactor = Redactor()
    out = redactor.redact("Andrea Di Cosmo ha risolto")
    # senza PII il nome resta (lo gestira' Presidio se attivo)
    assert "Andrea Di Cosmo" in out
