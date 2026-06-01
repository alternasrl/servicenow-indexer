"""Test del ServiceNowClient con una Session fittizia (nessuna rete reale)."""

from typing import List

from pipeline.config import ServiceNowConfig
from pipeline.servicenow import ServiceNowClient


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, headers=None):
        self.status_code = status_code
        self._json = json_data or {}
        self.headers = headers or {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            err = requests.HTTPError(f"HTTP {self.status_code}")
            err.response = self
            raise err


class FakeSession:
    """Restituisce risposte pre-programmate, registrando le chiamate."""

    def __init__(self, responses: List[FakeResponse]):
        self._responses = list(responses)
        self.calls = []
        self.auth = None
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params})
        return self._responses.pop(0)


def _config(page_size=2):
    return ServiceNowConfig(
        instance="acme",
        user="u",
        password="p",
        closed_states=["6", "7"],
        resolver_groups=["grp1"],
        configuration_items=["ci1"],
        page_size=page_size,
    )


def _rec(number, updated):
    return {
        "number": {"value": number, "display_value": number},
        "sys_updated_on": {"value": updated, "display_value": updated},
    }


def test_ping_returns_status():
    session = FakeSession([FakeResponse(200, {"result": []})])
    client = ServiceNowClient(_config(), session=session)
    assert client.ping() == 200
    assert session.calls[0]["url"].endswith("/api/now/table/incident")


def test_count_uses_aggregate_api():
    session = FakeSession(
        [FakeResponse(200, {"result": {"stats": {"count": "42"}}})]
    )
    client = ServiceNowClient(_config(), session=session)
    assert client.count() == 42
    assert "/api/now/stats/incident" in session.calls[0]["url"]
    assert session.calls[0]["params"]["sysparm_count"] == "true"


def test_iter_records_paginates_until_short_page():
    page1 = {"result": [_rec("INC1", "2024-01-01 00:00:00"),
                        _rec("INC2", "2024-01-02 00:00:00")]}
    page2 = {"result": [_rec("INC3", "2024-01-03 00:00:00")]}  # pagina corta -> stop
    session = FakeSession([FakeResponse(200, page1), FakeResponse(200, page2)])
    client = ServiceNowClient(_config(page_size=2), session=session)
    records = list(client.iter_records())
    assert [r["number"]["value"] for r in records] == ["INC1", "INC2", "INC3"]
    # due richieste di pagina
    assert len(session.calls) == 2
    assert session.calls[0]["params"]["sysparm_offset"] == "0"
    assert session.calls[1]["params"]["sysparm_offset"] == "2"


def test_iter_records_respects_max_records():
    page1 = {"result": [_rec("INC1", "2024-01-01 00:00:00"),
                        _rec("INC2", "2024-01-02 00:00:00")]}
    session = FakeSession([FakeResponse(200, page1)])
    client = ServiceNowClient(_config(page_size=200), session=session)
    records = list(client.iter_records(max_records=1))
    assert len(records) == 1
    # page_size limitato a max_records
    assert session.calls[0]["params"]["sysparm_limit"] == "1"


def test_request_page_retries_on_429(monkeypatch):
    import pipeline.servicenow as sn

    monkeypatch.setattr(sn.time, "sleep", lambda s: None)  # no attese reali
    session = FakeSession(
        [
            FakeResponse(429, headers={"Retry-After": "0"}),
            FakeResponse(200, {"result": [_rec("INC1", "2024-01-01 00:00:00")]}),
        ]
    )
    client = ServiceNowClient(_config(), session=session)
    records = list(client.iter_records())
    assert [r["number"]["value"] for r in records] == ["INC1"]
    assert len(session.calls) == 2  # primo 429, poi 200


def test_iter_records_sends_display_value_all():
    session = FakeSession([FakeResponse(200, {"result": []})])
    client = ServiceNowClient(_config(), session=session)
    list(client.iter_records())
    assert session.calls[0]["params"]["sysparm_display_value"] == "all"


# --- OAuth ---

def _oauth_config():
    cfg = _config()
    cfg.auth_mode = "oauth"
    cfg.oauth_client_id = "cid"
    cfg.oauth_client_secret = "csecret"
    return cfg


def test_oauth_provider_fetches_token_and_sets_header(monkeypatch):
    import pipeline.servicenow as sn

    posted = {}

    def fake_post(url, data=None, timeout=None):
        posted["url"] = url
        posted["data"] = data
        return FakeResponse(200, {"access_token": "AT1", "refresh_token": "RT1"})

    monkeypatch.setattr(sn.requests, "post", fake_post)

    session = FakeSession([FakeResponse(200, {"result": []})])
    client = ServiceNowClient(_oauth_config(), session=session)

    assert session.headers["Authorization"] == "Bearer AT1"
    assert posted["url"].endswith("/oauth_token.do")
    assert posted["data"]["grant_type"] == "password"
    assert posted["data"]["client_id"] == "cid"


def test_oauth_refreshes_token_on_401(monkeypatch):
    import pipeline.servicenow as sn

    tokens = iter(["AT1", "AT2"])

    def fake_post(url, data=None, timeout=None):
        return FakeResponse(
            200, {"access_token": next(tokens), "refresh_token": "RT1"}
        )

    monkeypatch.setattr(sn.requests, "post", fake_post)
    monkeypatch.setattr(sn.time, "sleep", lambda s: None)

    # prima un 401 (token scaduto), poi 200 dopo il refresh
    session = FakeSession(
        [FakeResponse(401), FakeResponse(200, {"result": [_rec("INC1", "2024-01-01 00:00:00")]})]
    )
    client = ServiceNowClient(_oauth_config(), session=session)
    records = list(client.iter_records())

    assert [r["number"]["value"] for r in records] == ["INC1"]
    # dopo il refresh l'header e' aggiornato col nuovo token
    assert session.headers["Authorization"] == "Bearer AT2"


def test_oauth_requires_client_credentials():
    cfg = _config()
    cfg.auth_mode = "oauth"  # senza client_id/secret
    import pytest

    with pytest.raises(NotImplementedError):
        ServiceNowClient(cfg, session=FakeSession([]))
