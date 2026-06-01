"""Test unitari sulla costruzione di query/filtro e watermark."""

from pipeline.config import ServiceNowConfig
from pipeline.servicenow import build_query
from pipeline.state import apply_overlap, max_watermark


def _config():
    return ServiceNowConfig(
        instance="acme",
        user="u",
        password="p",
        closed_states=["6", "7"],
        resolver_groups=["grp1", "grp2"],
        configuration_items=["ci1", "ci2"],
        overlap_minutes=15,
    )


def test_build_query_full_load_no_watermark():
    q = build_query(_config(), watermark=None)
    assert "stateIN6,7" in q
    assert "assignment_groupINgrp1,grp2" in q
    assert "cmdb_ciINci1,ci2" in q
    # nessuna clausola delta nel full load (l'ORDER BY su sys_updated_on resta)
    assert "sys_updated_on>=" not in q
    assert q.endswith("ORDERBYsys_updated_on")


def test_build_query_delta_includes_watermark():
    q = build_query(_config(), watermark="2024-03-01 10:30:00")
    assert "sys_updated_on>=javascript:gs.dateGenerate('2024-03-01','10:30:00')" in q
    # i filtri restano presenti
    assert "stateIN6,7" in q
    assert "assignment_groupINgrp1,grp2" in q
    assert "cmdb_ciINci1,ci2" in q


def test_build_query_optional_filters_omitted_when_empty():
    cfg = _config()
    cfg.resolver_groups = []
    cfg.configuration_items = []
    q = build_query(cfg, watermark=None)
    # senza filtri: solo stato chiuso + order by, niente clausole gruppi/CI
    assert "stateIN6,7" in q
    assert "assignment_group" not in q
    assert "cmdb_ci" not in q
    assert q.endswith("ORDERBYsys_updated_on")


def test_build_query_only_resolver_groups():
    cfg = _config()
    cfg.configuration_items = []
    q = build_query(cfg, watermark=None)
    assert "assignment_groupINgrp1,grp2" in q
    assert "cmdb_ci" not in q


def test_build_query_clause_order():
    q = build_query(_config(), watermark="2024-03-01 10:30:00")
    # i filtri sono uniti da ^ e l'order by e' in coda
    parts = q.split("^")
    assert parts[0] == "stateIN6,7"
    assert parts[-1] == "ORDERBYsys_updated_on"


def test_base_url_from_instance_name():
    assert _config().base_url == "https://acme.service-now.com"


def test_base_url_passthrough_when_url_given():
    cfg = _config()
    cfg.instance = "https://acme.service-now.com"
    assert cfg.base_url == "https://acme.service-now.com"


def test_apply_overlap_subtracts_minutes():
    assert apply_overlap("2024-03-01 10:30:00", 15) == "2024-03-01 10:15:00"


def test_apply_overlap_none():
    assert apply_overlap(None, 15) is None


def test_max_watermark():
    assert max_watermark("2024-03-01 10:00:00", "2024-03-01 11:00:00") == (
        "2024-03-01 11:00:00"
    )
    assert max_watermark("2024-03-01 12:00:00", "2024-03-01 11:00:00") == (
        "2024-03-01 12:00:00"
    )
    assert max_watermark(None, "2024-03-01 11:00:00") == "2024-03-01 11:00:00"
    assert max_watermark("2024-03-01 11:00:00", None) == "2024-03-01 11:00:00"
