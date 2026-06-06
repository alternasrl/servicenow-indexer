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
        reasoning_effort: Optional[str] = None,
        max_completion_tokens: int = 4000,
    ) -> None:
        self.endpoint = endpoint
        self.api_key = api_key
        self.deployment = deployment
        self.api_version = api_version
        self.mask = mask
        self.max_chars = max_chars
        # Parametri specifici dei modelli reasoning gpt-5 (es. gpt-5-mini):
        # usano max_completion_tokens (non max_tokens), non accettano temperature
        # custom, e accettano reasoning_effort (minimal = veloce per estrazione).
        self.reasoning_effort = reasoning_effort
        self.max_completion_tokens = max_completion_tokens
        self._injected_client = client  # client esplicito (test): condiviso
        # Contatori thread-safe (la redaction gira in parallelo).
        import threading

        self._lock = threading.Lock()
        # Client PER-THREAD: evita il deadlock dell'httpx connection pool quando
        # un singolo client viene condiviso tra molti thread sotto retry/429.
        self._tlocal = threading.local()
        # Watchdog per-chiamata: il timeout del client httpx a volte NON scatta
        # (socket appeso su Windows) e congela la fase PII. Eseguiamo la chiamata
        # in un thread separato con join(timeout): se sfora, abbandona e ritenta.
        self._call_timeout = float(os.environ.get("PII_CALL_TIMEOUT_S", "45"))
        self.values_masked = 0  # quanti VALORI PII distinti mascherati
        self.occurrences_masked = 0  # quante OCCORRENZE totali sostituite
        self.docs_with_pii = 0  # quanti documenti contenevano PII
        self.failures = 0  # quante redaction sono FALLITE (testo non redatto!)

    @property
    def stats(self) -> dict:
        return {
            "values_masked": self.values_masked,
            "occurrences_masked": self.occurrences_masked,
            "docs_with_pii": self.docs_with_pii,
            "failures": self.failures,
        }

    def _ensure_client(self):
        # Test: client iniettato e condiviso.
        if self._injected_client is not None:
            return self._injected_client
        # Produzione: un client per THREAD (connection pool isolato).
        client = getattr(self._tlocal, "client", None)
        if client is None:
            from openai import AzureOpenAI  # import lazy

            # timeout duro (la richiesta non resta appesa) + retry sui 429.
            client = AzureOpenAI(
                azure_endpoint=self.endpoint,
                api_key=self.api_key,
                api_version=self.api_version,
                timeout=30.0,
                max_retries=6,
            )
            self._tlocal.client = client
        return client

    def _extract_pii(self, text: str, client=None) -> list:
        """Chiede all'LLM la lista dei valori PII presenti nel testo (JSON).

        Usa max_completion_tokens (compatibile sia coi modelli gpt-4o sia coi
        reasoning gpt-5) e, se configurato, reasoning_effort. Niente temperature
        custom (i gpt-5 la rifiutano).
        """
        import json

        client = client or self._ensure_client()
        kwargs = dict(
            model=self.deployment,
            max_completion_tokens=self.max_completion_tokens,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": LLM_PII_SYSTEM_PROMPT.format(mask=self.mask)},
                {"role": "user", "content": text},
            ],
        )
        if self.reasoning_effort:
            kwargs["reasoning_effort"] = self.reasoning_effort
        resp = client.chat.completions.create(**kwargs)
        content = resp.choices[0].message.content or "{}"
        data = json.loads(content)
        raw = data.get("pii", []) if isinstance(data, dict) else []
        # Parsing tollerante: i valori possono essere stringhe oppure dict
        # {"type":..., "value":...} (gpt-5-mini a volte risponde cosi').
        values = []
        for item in raw:
            if isinstance(item, str):
                values.append(item)
            elif isinstance(item, dict):
                v = item.get("value") or item.get("text") or item.get("pii")
                if isinstance(v, str):
                    values.append(v)
        # Deduplicate, ordina per lunghezza decrescente (prima i valori piu'
        # lunghi: evita match parziali).
        uniq = {v for v in values if v and v.strip()}
        return sorted(uniq, key=len, reverse=True)

    class RedactionError(RuntimeError):
        """Sollevata quando la redaction PII fallisce (testo NON redatto)."""

    def _extract_pii_watchdog(self, text: str) -> list:
        """Esegue _extract_pii in un thread con timeout (join).

        Se la chiamata si appende oltre il timeout, il thread viene abbandonato
        (daemon, muore col processo), il client thread-local viene invalidato e
        si solleva TimeoutError: la fase PII non puo' piu' congelarsi.
        """
        import threading

        # Client del thread chiamante (riusato tra le chiamate dello stesso
        # worker), passato al daemon per non crearne uno nuovo ad ogni chiamata.
        client = self._ensure_client()
        box = {}

        def work():
            try:
                box["v"] = self._extract_pii(text, client=client)
            except Exception as exc:  # noqa
                box["e"] = exc

        t = threading.Thread(target=work, daemon=True)
        t.start()
        t.join(self._call_timeout)
        if t.is_alive():
            # chiamata appesa: il client e' probabilmente compromesso -> lo
            # invalido per il thread chiamante (la prossima chiamata ne crea uno
            # nuovo) e segnalo timeout.
            self._tlocal.client = None
            raise TimeoutError(f"estrazione PII appesa > {self._call_timeout}s")
        if "e" in box:
            raise box["e"]
        return box.get("v", [])

    def redact(self, text: str | None, language: str = "it", raise_on_error: bool = False) -> str:
        if not text:
            return ""
        snippet = text if len(text) <= self.max_chars else text[: self.max_chars]
        try:
            pii_values = self._extract_pii_watchdog(snippet)
        except Exception as exc:
            with self._lock:
                self.failures += 1
            logger.warning("Redaction PII FALLITA (testo NON redatto): %s", exc)
            if raise_on_error:
                raise LlmPiiRedactor.RedactionError(str(exc)) from exc
            return text
        if not pii_values:
            return text
        out = text
        occ = 0
        for value in pii_values:
            count = out.count(value)
            if count:
                out = out.replace(value, self.mask)
                occ += count
        # Aggiorna i contatori in modo thread-safe.
        with self._lock:
            self.values_masked += len(pii_values)
            self.occurrences_masked += occ
            if occ:
                self.docs_with_pii += 1
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
    # reasoning_effort: per i modelli gpt-5 (es. "minimal"). Vuoto = non inviato.
    reasoning_effort = os.environ.get("PII_LLM_REASONING_EFFORT") or None
    max_completion_tokens = int(os.environ.get("PII_LLM_MAX_COMPLETION_TOKENS", "4000"))
    if not endpoint or not api_key:
        logger.warning(
            "PII backend 'llm' richiede PII_LLM_ENDPOINT/API_KEY (o AOAI_*). "
            "Redaction PII DISATTIVATA."
        )
        return None
    logger.info(
        "PII backend: LLM Foundry (deployment=%s, reasoning_effort=%s)",
        deployment, reasoning_effort,
    )
    return LlmPiiRedactor(
        endpoint=endpoint,
        api_key=api_key,
        deployment=deployment,
        api_version=api_version,
        reasoning_effort=reasoning_effort,
        max_completion_tokens=max_completion_tokens,
    )
