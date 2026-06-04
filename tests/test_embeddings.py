"""Test sull'EmbeddingClient (con client OpenAI fittizio)."""

from pipeline.config import EmbeddingConfig
from pipeline.embeddings import EmbeddingClient


class FakeEmbeddingItem:
    def __init__(self, index, embedding):
        self.index = index
        self.embedding = embedding


class FakeResponse:
    def __init__(self, data):
        self.data = data


class FakeOpenAI:
    """Registra gli input ricevuti e ritorna vettori fittizi."""

    def __init__(self, dims=4):
        self.dims = dims
        self.received_inputs = []

        class _Emb:
            def __init__(self, outer):
                self._outer = outer

            def create(self, model, input, dimensions):
                self._outer.received_inputs = list(input)
                data = [
                    FakeEmbeddingItem(i, [0.1] * dimensions)
                    for i in range(len(input))
                ]
                return FakeResponse(data)

        self.embeddings = _Emb(self)


def _config(max_chars=100):
    return EmbeddingConfig(
        endpoint="https://x.openai.azure.com",
        api_key="k",
        deployment="dep",
        dimensions=4,
        batch_size=16,
        max_input_chars=max_chars,
    )


def test_embed_adds_vector_to_documents():
    fake = FakeOpenAI()
    client = EmbeddingClient(_config(), client=fake)
    docs = [{"content": "ciao"}, {"content": "mondo"}]
    out = client.embed_documents(docs)
    assert all("contentVector" in d for d in out)
    assert len(out[0]["contentVector"]) == 4


def test_embed_skips_empty_content():
    fake = FakeOpenAI()
    client = EmbeddingClient(_config(), client=fake)
    docs = [{"content": ""}, {"content": "x"}]
    out = client.embed_documents(docs)
    # solo il secondo riceve il vettore
    assert "contentVector" not in out[0]
    assert "contentVector" in out[1]


def test_embed_truncates_by_tokens():
    fake = FakeOpenAI()
    cfg = _config()
    cfg.max_input_tokens = 50  # limite basso per il test
    client = EmbeddingClient(cfg, client=fake)
    long_text = "parola " * 5000  # molti token
    client.embed_documents([{"content": long_text}])
    sent = fake.received_inputs[0]
    # se tiktoken e' presente, il testo inviato non supera ~max_input_tokens
    if client._encoding is not None:
        n_tokens = len(client._encoding.encode(sent))
        assert n_tokens <= 50
    else:  # fallback per caratteri
        assert len(sent) <= cfg.max_input_chars


def test_embed_truncates_by_chars_when_no_tiktoken():
    fake = FakeOpenAI()
    cfg = _config(max_chars=10)
    client = EmbeddingClient(cfg, client=fake)
    client._encoding = None  # forza il fallback
    client.embed_documents([{"content": "A" * 5000}])
    assert len(fake.received_inputs[0]) == 10
