from datetime import UTC, datetime, timedelta, timezone

import pytest

from oam.profiles.resources import map_profile_data


def test_basic_profile_maps_registered_fields_with_exact_resource_support() -> None:
    fields = map_profile_data(
        {
            "resource_id": "user-basic-profile",
            "resource_type": "profile",
            "subtype": "basic-profile/v1",
            "source_revision": "12",
            "as_of": "2026-09-19T09:00:00Z",
            "content": {
                "date_of_birth": "1990-05-10",
                "sex": "female",
                "household_size": 4,
                "interests": ["hiking", "photography"],
            },
        },
        resource_version=3,
    )

    assert [(field.section, field.key, field.value, field.qualifier) for field in fields] == [
        ("main", "date_of_birth", "1990-05-10", None),
        ("main", "sex", "female", None),
        ("main", "household_size", 4, None),
        ("preferences", "interests", ["hiking", "photography"], None),
    ]
    assert all(field.assertion_kind == "stated" for field in fields)
    assert all(field.support.resource_id == "user-basic-profile" for field in fields)
    assert all(field.support.resource_version == 3 for field in fields)
    assert all(field.support.source_revision == "12" for field in fields)
    assert all(field.support.as_of == datetime(2026, 9, 19, 9, tzinfo=UTC) for field in fields)
    assert [field.support.content_path for field in fields] == [
        "content.date_of_birth",
        "content.sex",
        "content.household_size",
        "content.interests",
    ]


def test_preference_domain_qualifies_exact_language_value() -> None:
    fields = map_profile_data(
        {
            "resource_id": "user-preference-examples",
            "resource_type": "profile",
            "subtype": "preferences/v1",
            "source_revision": "2",
            "as_of": "2026-07-11T09:00:00+08:00",
            "content": {"example_language": "Python", "domain": "backend"},
        },
        resource_version=1,
    )

    assert len(fields) == 1
    assert fields[0].section == "preferences"
    assert fields[0].key == "example_language"
    assert fields[0].value == "Python"
    assert fields[0].qualifier == "backend"
    assert fields[0].support.content_path == "content.example_language"
    assert fields[0].support.as_of == datetime(2026, 7, 11, 9, tzinfo=timezone(timedelta(hours=8)))


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"resource_type": "evidence"}, "resource_type"),
        ({"subtype": "unknown/v1"}, "subtype"),
        ({"content": {"age": 35}}, "content"),
        ({"content": {"household_size": True}}, "household_size"),
        ({"content": {"date_of_birth": "1990-02-30"}}, "date_of_birth"),
        ({"content": {"interests": ["hiking", 7]}}, "interests"),
        ({"as_of": "2026-09-19T09:00:00"}, "as_of"),
    ],
)
def test_basic_profile_rejects_unregistered_or_malformed_input(change: dict, message: str) -> None:
    resource = {
        "resource_id": "user-basic-profile",
        "resource_type": "profile",
        "subtype": "basic-profile/v1",
        "source_revision": "12",
        "as_of": "2026-09-19T09:00:00Z",
        "content": {"sex": "female"},
    }
    resource.update(change)

    with pytest.raises(ValueError, match=message):
        map_profile_data(resource, resource_version=1)


def test_profile_resource_rejects_invalid_version() -> None:
    resource = {
        "resource_id": "user-preference-examples",
        "resource_type": "profile",
        "subtype": "preferences/v1",
        "source_revision": "2",
        "as_of": "2026-07-11T09:00:00+08:00",
        "content": {"example_language": "Python", "domain": "backend"},
    }

    with pytest.raises(ValueError, match="resource_version"):
        map_profile_data(resource, resource_version=0)
