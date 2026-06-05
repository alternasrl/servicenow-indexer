"""Redaction centralizzata e testabile.

I ticket dell'help desk Oracle contengono spesso credenziali e dati sensibili
nelle note. La redaction viene applicata SEMPRE prima della scrittura su Azure
AI Search e prima del calcolo degli embedding, cosi' nessun segreto entra
nell'indice ne' viene inviato ad Azure OpenAI.

Aggiungere nuovi pattern (PII, ecc.) significa solo estendere la lista
`DEFAULT_PATTERNS` oppure passarne una custom a `Redactor`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Pattern

MASK = "[REDACTED]"


@dataclass
class RedactionRule:
    name: str
    pattern: Pattern[str]
    # Template di sostituzione: puo' usare i gruppi catturati (es. r"\1=" + MASK)
    replacement: str


def _rule(name: str, regex: str, replacement: str, flags: int = re.IGNORECASE) -> RedactionRule:
    return RedactionRule(name=name, pattern=re.compile(regex, flags), replacement=replacement)


# Ordine: i pattern piu' specifici (connection string) prima di quelli generici.
DEFAULT_PATTERNS: List[RedactionRule] = [
    # Stringhe di connessione tipo Oracle EZConnect: utente/password@host:porta/servizio
    # Maschera solo la password, preserva utente/host/servizio (utili per contesto).
    _rule(
        "oracle_connection_string",
        r"\b([A-Za-z0-9_.$#]+)/[^/@\s]+@([A-Za-z0-9_.\-]+(?::\d+)?(?:/[A-Za-z0-9_.\-]+)?)",
        r"\1/" + MASK + r"@\2",
    ),
    # Clausola Oracle: IDENTIFIED BY <password>  /  IDENTIFIED BY VALUES '<hash>'
    _rule(
        "oracle_identified_by",
        r"(IDENTIFIED\s+BY\s+)(?:VALUES\s+)?(?:'[^']*'|\"[^\"]*\"|[^\s;]+)",
        r"\1" + MASK,
    ),
    # password=... / pwd=... / pass: ... (separatore = o :)
    _rule(
        "password_assignment",
        r"\b(pass(?:word)?|pwd)\b\s*[:=]\s*(?:'[^']*'|\"[^\"]*\"|\S+)",
        r"\1=" + MASK,
    ),
]


class Redactor:
    """Applica la redaction a stringhe, in due stadi:

    1. regole REGEX (segreti con pattern fisso: password, connection string);
    2. (opzionale) redaction PII via Presidio (nomi, email, telefoni, IBAN/CF).

    Il secondo stadio si attiva solo se viene fornito un `pii_redactor`
    (tipicamente costruito da env con PII_REDACTION_ENABLED=true). Se assente,
    il comportamento e' identico alla sola redaction regex.
    """

    def __init__(
        self,
        rules: List[RedactionRule] | None = None,
        pii_redactor=None,
    ) -> None:
        self.rules = rules if rules is not None else DEFAULT_PATTERNS
        self.pii_redactor = pii_redactor

    def redact(self, text: str | None, language: str = "it") -> str:
        if not text:
            return ""
        out = text
        # Stadio 1: regex (sempre).
        for rule in self.rules:
            out = rule.pattern.sub(rule.replacement, out)
        # Stadio 2: PII via Presidio (se configurato).
        if self.pii_redactor is not None:
            out = self.pii_redactor.redact(out, language=language)
        return out


# Istanza di default riutilizzabile.
# Se PII_REDACTION_ENABLED e' attivo, aggancia anche la redaction PII.
def _build_default_redactor() -> "Redactor":
    pii = None
    try:
        from .pii import build_pii_redactor_from_env

        pii = build_pii_redactor_from_env()
    except Exception:  # pragma: no cover - pii e' opzionale
        pii = None
    return Redactor(pii_redactor=pii)


default_redactor = _build_default_redactor()


def redact(text: str | None, language: str = "it") -> str:
    """Helper modulo che usa il redactor di default."""
    return default_redactor.redact(text, language=language)
