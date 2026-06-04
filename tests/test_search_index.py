"""Test sullo schema dell'indice e sulla logica di autenticazione."""

from pipeline.config import EmbeddingConfig, SearchConfig
from pipeline.search_index import build_index_definition


def _embedding():
    return EmbeddingConfig(
        endpoint="https://x.openai.azure.com",
        api_key="k",
        deployment="dep",
        model="text-embedding-3-small",
        dimensions=1536,
    )


def test_index_has_expected_fields():
    d = build_index_definition("oracle-hd-kb", _embedding())
    names = {f["name"] for f in d["fields"]}
    expected = {
        "id",
        "number",
        "url",
        "short_description",
        "description",
        "resolution",
        "work_notes",
        "comments",
        "content",
        "assignment_group",
        "assignment_group_name",
        "priority",
        "impact",
        "urgency",
        "closed_at",
        "contentVector",
    }
    assert expected.issubset(names)
    # cmdb_ci rimosso
    assert "cmdb_ci" not in names
    assert "cmdb_ci_name" not in names


def test_key_field_is_id():
    d = build_index_definition("idx", _embedding())
    key_fields = [f["name"] for f in d["fields"] if f.get("key")]
    assert key_fields == ["id"]


def test_vector_field_dimensions_match_config():
    emb = _embedding()
    emb.dimensions = 3072
    d = build_index_definition("idx", emb)
    vec = next(f for f in d["fields"] if f["name"] == "contentVector")
    assert vec["dimensions"] == 3072
    assert vec["type"] == "Collection(Edm.Single)"
    assert vec["retrievable"] is False


def test_vectorizer_points_to_deployment():
    d = build_index_definition("idx", _embedding())
    vectorizers = d["vectorSearch"]["vectorizers"]
    params = vectorizers[0]["azureOpenAIParameters"]
    assert params["deploymentId"] == "dep"
    assert params["modelName"] == "text-embedding-3-small"


def test_semantic_config_present():
    d = build_index_definition("idx", _embedding())
    sem = d["semantic"]["configurations"][0]
    assert sem["prioritizedFields"]["titleField"]["fieldName"] == "short_description"
    content_fields = sem["prioritizedFields"]["prioritizedContentFields"]
    assert {"fieldName": "content"} in content_fields


def test_journal_fields_not_retrievable():
    d = build_index_definition("idx", _embedding())
    for name in ("work_notes", "comments"):
        f = next(x for x in d["fields"] if x["name"] == name)
        assert f["searchable"] is True
        assert f["retrievable"] is False


def test_searchconfig_requires_key_or_aad(monkeypatch):
    import pytest

    from pipeline.config import ConfigError

    # nessuna key e nessun AAD -> errore
    monkeypatch.setenv("SEARCH_ENDPOINT", "https://x.search.windows.net")
    monkeypatch.setenv("SEARCH_INDEX_NAME", "idx")
    monkeypatch.delenv("SEARCH_ADMIN_KEY", raising=False)
    monkeypatch.delenv("SEARCH_USE_AAD", raising=False)
    with pytest.raises(ConfigError):
        SearchConfig.from_env()


def test_searchconfig_aad_ok_without_key(monkeypatch):
    monkeypatch.setenv("SEARCH_ENDPOINT", "https://x.search.windows.net")
    monkeypatch.setenv("SEARCH_INDEX_NAME", "idx")
    monkeypatch.setenv("SEARCH_USE_AAD", "true")
    monkeypatch.delenv("SEARCH_ADMIN_KEY", raising=False)
    cfg = SearchConfig.from_env()
    assert cfg.use_aad is True
    assert cfg.admin_key == ""
