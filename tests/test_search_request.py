from __future__ import annotations

from app.services.search_request import DEFAULT_ADMIN_FIELDS, build_search_query


def test_build_search_query_uses_shared_admin_defaults():
    request = build_search_query("食堂", "time", 2, 20, admin=True)

    assert request.text == "食堂"
    assert request.page == 2
    assert request.limit == 20
    assert request.admin_fields == DEFAULT_ADMIN_FIELDS


def test_build_search_query_preserves_explicit_empty_admin_fields():
    request = build_search_query("食堂", "time", 1, 20, admin=True, admin_fields=set())

    assert request.admin_fields == DEFAULT_ADMIN_FIELDS


def test_build_search_query_maps_public_service_names_to_domain_fields():
    request = build_search_query(
        "u-1",
        "time",
        1,
        20,
        uid="u-1",
        uname="昵称",
        id_match="contains",
        name_match="contains",
    )

    assert request.user_id == "u-1"
    assert request.user_name == "昵称"
    assert request.id_match == "contains"
    assert request.name_match == "contains"
