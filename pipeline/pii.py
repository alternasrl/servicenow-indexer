"""Redaction PII via Microsoft Presidio (NER multilingua IT/EN).

Complementa la redaction regex (`redaction.py`): le regex coprono pattern fissi
(password, connection string), Presidio copre entita' senza pattern fisso come
nomi di persona, email, telefoni, IBAN/codici fiscali.

E' un componente OPZIONALE e a caricamento lazy:
- si attiva con PII_REDACTION_ENABLED=true;
- se Presidio o i modelli spaCy non sono installati, NON blocca la pipeline:
  logga un warning e disattiva la redaction PII (fallback sicuro alle sole regex).

Modelli richiesti (scaricabili una volta):
    python -m spacy download en_core_web_lg
    python -m spacy download it_core_news_lg
"""

from __future__ import annotations

import logging
import os
from typing import List, Optional

logger = logging.getLogger(__name__)

MASK = "[PII]"

# Entita' Presidio supportate di default per l'help desk Oracle.
DEFAULT_ENTITIES = [
    "PERSON",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "IBAN_CODE",
    "IT_FISCAL_CODE",
]

# Soglie di confidenza per entita'. PERSON a 0.4 per catturare anche nomi
# isolati (es. "Ciao Chiara"), a costo di qualche falso positivo: scelta
# prudente in ottica privacy (meglio mascherare in piu' che lasciar passare un
# nome). Sovrascrivibile via env PII_PERSON_THRESHOLD.
DEFAULT_THRESHOLDS = {
    "PERSON": 0.4,
    "EMAIL_ADDRESS": 0.5,
    "PHONE_NUMBER": 0.4,
    "IBAN_CODE": 0.5,
    "IT_FISCAL_CODE": 0.5,
}


def _csv_env(name: str, default: List[str]) -> List[str]:
    raw = os.environ.get(name)
    if not raw:
        return default
    return [x.strip() for x in raw.split(",") if x.strip()]


class PiiRedactor:
    """Wrapper su Presidio per la redaction PII multilingua (IT/EN).

    Il caricamento di Presidio/spaCy e' lazy (al primo `redact`), cosi' importare
    il modulo non ha costo se la PII non viene usata.
    """

    def __init__(
        self,
        entities: Optional[List[str]] = None,
        languages: Optional[List[str]] = None,
        thresholds: Optional[dict] = None,
        mask: str = MASK,
    ) -> None:
        self.entities = entities or DEFAULT_ENTITIES
        self.languages = languages or ["it", "en"]
        self.thresholds = thresholds or DEFAULT_THRESHOLDS
        self.mask = mask
        self._analyzer = None
        self._anonymizer = None
        self._operators = None
        self._available = None  # None = non ancora inizializzato

    # Mappa codice lingua -> modello spaCy "large".
    _MODEL_BY_LANG = {
        "en": "en_core_web_lg",
        "it": "it_core_news_lg",
    }

    def _ensure_engine(self) -> bool:
        """Inizializza Presidio. Ritorna True se disponibile, False altrimenti."""
        if self._available is not None:
            return self._available
        try:
            from presidio_analyzer import AnalyzerEngine
            from presidio_analyzer.nlp_engine import NlpEngineProvider
            from presidio_anonymizer import AnonymizerEngine
            from presidio_anonymizer.entities import OperatorConfig

            models = [
                {"lang_code": lang, "model_name": self._MODEL_BY_LANG[lang]}
                for lang in self.languages
                if lang in self._MODEL_BY_LANG
            ]
            provider = NlpEngineProvider(
                nlp_configuration={"nlp_engine_name": "spacy", "models": models}
            )
            nlp_engine = provider.create_engine()
            self._analyzer = AnalyzerEngine(
                nlp_engine=nlp_engine, supported_languages=self.languages
            )
            self._anonymizer = AnonymizerEngine()
            self._operators = {
                "DEFAULT": OperatorConfig("replace", {"new_value": self.mask})
            }
            self._available = True
            logger.info(
                "Presidio PII attivo (lingue=%s, entita=%s)",
                self.languages,
                self.entities,
            )
        except Exception as exc:  # pragma: no cover - dipende dall'ambiente
            logger.warning(
                "Presidio/modelli non disponibili: redaction PII DISATTIVATA "
                "(solo regex). Dettaglio: %s",
                exc,
            )
            self._available = False
        return self._available

    def redact(self, text: str | None, language: str = "it") -> str:
        if not text:
            return ""
        if not self._ensure_engine():
            return text  # fallback: nessuna PII redaction
        lang = language if language in self.languages else self.languages[0]
        try:
            results = self._analyzer.analyze(
                text=text, language=lang, entities=self.entities
            )
            # Filtra per soglia di confidenza per-entita'.
            results = [
                r
                for r in results
                if r.score >= self.thresholds.get(r.entity_type, 0.5)
            ]
            if not results:
                return text
            anonymized = self._anonymizer.anonymize(
                text=text, analyzer_results=results, operators=self._operators
            )
            return anonymized.text
        except Exception as exc:  # pragma: no cover
            logger.warning("Errore redaction PII, testo lasciato invariato: %s", exc)
            return text


# --- Backend alternativo: LLM su Azure OpenAI / Foundry ---------------------

# Prompt "estrai, non riscrivere": l'LLM restituisce SOLO la lista dei valori
# PII trovati (JSON), che vengono poi sostituiti localmente. Cosi' l'output e'
# di pochi token (veloce, economico, niente rischio di alterare il testo).
LLM_PII_SYSTEM_PROMPT = """Sei un estrattore di dati personali (PII) da ticket di \
help desk IT (Oracle). Ricevi un testo e devi restituire un oggetto JSON con la \
lista ESATTA dei valori PII presenti nel testo, da anonimizzare.

Includi in "pii" (copiando il valore esatto come appare nel testo):
- nomi e cognomi di persone (anche isolati, in italiano o inglese);
- indirizzi email;
- numeri di telefono;
- IBAN e codici fiscali.

NON includere (NON sono PII): nomi di tabelle, job, interfacce, codici prodotto, \
numeri di ticket (INC...), nomi di gruppi/team, prodotti software, parole comuni, \
date, importi, URL.

Rispondi SOLO con JSON valido nel formato: {{"pii": ["valore1", "valore2", ...]}}. \
Se non ci sono PII, rispondi {{"pii": []}}."""


class LlmPiiRedactor:
    """Redaction PII tramite un LLM chat su Azure OpenAI / Foundry.

    Vantaggi rispetto al NER: comprensione del contesto (nomi isolati, IT/EN),
    nessun modello locale da distribuire (Function leggera). Caricamento lazy del
    client; in caso di errore lascia il testo invariato (fail-safe) e logga.
    """

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        deployment: str,
        api_version: str = "2024-06-01",
        mask: str = MASK,
        client=None,
        max_chars: int = 24000,
    ) -> None:
        self.endpoint = endpoint
        self.api_key = api_key
        self.deployment = deployment
        self.api_version = api_version
        self.mask = mask
        self.max_chars = max_chars
        self._client = client

    def _ensure_client(self):
        if self._client is None:
            from openai import AzureOpenAI  # import lazy

            self._client = AzureOpenAI(
                azure_endpoint=self.endpoint,
                api_key=self.api_key,
                api_version=self.api_version,
            )
        return self._client

    def _extract_pii(self, text: str) -> list:
        """Chiede all'LLM la lista dei valori PII presenti nel testo (JSON)."""
        import json

        client = self._ensure_client()
        resp = client.chat.completions.create(
            model=self.deployment,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": LLM_PII_SYSTEM_PROMPT.format(mask=self.mask)},
                {"role": "user", "content": text},
            ],
        )
        content = resp.choices[0].message.content or "{}"
        data = json.loads(content)
        values = data.get("pii", []) if isinstance(data, dict) else []
        # Solo stringhe non vuote, deduplicate, ordinate per lunghezza decrescente
        # (sostituisco prima i valori piu' lunghi: evita match parziali).
        uniq = {v for v in values if isinstance(v, str) and v.strip()}
        return sorted(uniq, key=len, reverse=True)

    def redact(self, text: str | None, language: str = "it") -> str:
        if not text:
            return ""
        snippet = text if len(text) <= self.max_chars else text[: self.max_chars]
        try:
            pii_values = self._extract_pii(snippet)
        except Exception as exc:  # pragma: no cover - dipende dal servizio
            logger.warning("Errore estrazione PII via LLM, testo invariato: %s", exc)
            return text
        if not pii_values:
            return text
        out = text
        for value in pii_values:
            out = out.replace(value, self.mask)
        return out


def build_pii_redactor_from_env():
    """Crea il redactor PII se PII_REDACTION_ENABLED e' attivo, altrimenti None.

    Backend selezionabile con PII_BACKEND:
    - "llm" (default): usa un modello chat Azure OpenAI/Foundry (PII_LLM_*);
    - "presidio": usa Presidio + spaCy (NER locale).
    """
    enabled = (os.environ.get("PII_REDACTION_ENABLED", "false") or "").strip().lower()
    if enabled not in ("1", "true", "yes"):
        return None

    backend = (os.environ.get("PII_BACKEND", "llm") or "llm").strip().lower()

    if backend == "presidio":
        entities = _csv_env("PII_ENTITIES", DEFAULT_ENTITIES)
        languages = _csv_env("PII_LANGUAGES", ["it", "en"])
        thresholds = dict(DEFAULT_THRESHOLDS)
        raw_person = os.environ.get("PII_PERSON_THRESHOLD")
        if raw_person:
            try:
                thresholds["PERSON"] = float(raw_person)
            except ValueError:
                logger.warning("PII_PERSON_THRESHOLD non valido: %s", raw_person)
        return PiiRedactor(entities=entities, languages=languages, thresholds=thresholds)

    # default: LLM. Riusa per default le credenziali Azure OpenAI degli embedding.
    endpoint = os.environ.get("PII_LLM_ENDPOINT") or os.environ.get("AOAI_ENDPOINT")
    api_key = os.environ.get("PII_LLM_API_KEY") or os.environ.get("AOAI_API_KEY")
    deployment = os.environ.get("PII_LLM_DEPLOYMENT", "gpt-4o-mini")
    api_version = os.environ.get("PII_LLM_API_VERSION", "2024-06-01")
    if not endpoint or not api_key:
        logger.warning(
            "PII backend 'llm' richiede PII_LLM_ENDPOINT/API_KEY (o AOAI_*). "
            "Redaction PII DISATTIVATA."
        )
        return None
    logger.info("PII backend: LLM Foundry (deployment=%s)", deployment)
    return LlmPiiRedactor(
        endpoint=endpoint,
        api_key=api_key,
        deployment=deployment,
        api_version=api_version,
    )
