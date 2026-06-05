"""Test del backend PII basato su LLM (client Azure OpenAI fittizio)."""

from pipeline.pii import LlmPiiRedactor, build_pii_redactor_from_env


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeCompletion:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeChatNS:
    def __init__(self, outer):
        self._outer = outer
        self.completions = self

    def create(self, model, messages, temperature=0, response_format=None):
        self._outer.last_messages = messages
        self._outer.last_model = model
        # Simula l'estrazione PII: ritorna JSON con i nomi noti trovati nel testo.
        import json

        user_text = messages[-1]["content"]
        found = [n for n in ("Mario Rossi", "Andrea Di Cosmo") if n in user_text]
        return _FakeCompletion(json.dumps({"pii": found}))


class FakeOpenAI:
    def __init__(self):
        self.chat = _FakeChatNS(self)
        self.last_messages = None
        self.last_model = None


def test_llm_redactor_masks_via_model():
    fake = FakeOpenAI()
    r = LlmPiiRedactor(
        endpoint="https://x.openai.azure.com",
        api_key="k",
        deployment="gpt-4o-mini",
        client=fake,
    )
    out = r.redact("Ticket gestito da Mario Rossi")
    assert out == "Ticket gestito da [PII]"
    assert fake.last_model == "gpt-4o-mini"
    # il system prompt e' presente
    assert fake.last_messages[0]["role"] == "system"


def test_llm_redactor_empty_text():
    fake = FakeOpenAI()
    r = LlmPiiRedactor("e", "k", "dep", client=fake)
    assert r.redact("") == ""
    assert r.redact(None) == ""


def test_llm_redactor_failsafe_on_error():
    class Boom:
        def __init__(self):
            self.chat = self
            self.completions = self

        def create(self, **kwargs):
            raise RuntimeError("service down")

    r = LlmPiiRedactor("e", "k", "dep", client=Boom())
    # in caso di errore restituisce il testo invariato (fail-safe)
    assert r.redact("Mario Rossi") == "Mario Rossi"


def test_llm_redactor_replaces_all_occurrences():
    fake = FakeOpenAI()
    r = LlmPiiRedactor("e", "k", "dep", client=fake)
    # un nome che compare piu' volte deve essere mascherato ovunque
    out = r.redact("Mario Rossi ha aperto, poi Mario Rossi ha chiuso")
    assert "Mario Rossi" not in out
    assert out.count("[PII]") == 2


def test_build_from_env_selects_llm(monkeypatch):
    monkeypatch.setenv("PII_REDACTION_ENABLED", "true")
    monkeypatch.setenv("PII_BACKEND", "llm")
    monkeypatch.setenv("AOAI_ENDPOINT", "https://x.openai.azure.com")
    monkeypatch.setenv("AOAI_API_KEY", "k")
    monkeypatch.setenv("PII_LLM_DEPLOYMENT", "gpt-4o-mini")
    r = build_pii_redactor_from_env()
    assert isinstance(r, LlmPiiRedactor)
    assert r.deployment == "gpt-4o-mini"


def test_build_from_env_disabled(monkeypatch):
    monkeypatch.setenv("PII_REDACTION_ENABLED", "false")
    assert build_pii_redactor_from_env() is None


def test_build_from_env_llm_missing_creds(monkeypatch):
    monkeypatch.setenv("PII_REDACTION_ENABLED", "true")
    monkeypatch.setenv("PII_BACKEND", "llm")
    monkeypatch.delenv("AOAI_ENDPOINT", raising=False)
    monkeypatch.delenv("AOAI_API_KEY", raising=False)
    monkeypatch.delenv("PII_LLM_ENDPOINT", raising=False)
    monkeypatch.delenv("PII_LLM_API_KEY", raising=False)
    # senza credenziali -> disattivato (None), non esplode
    assert build_pii_redactor_from_env() is None
