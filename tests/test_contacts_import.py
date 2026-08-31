from sqlalchemy import func, select

from zerocrm.contacts_import import import_contacts_csv
from zerocrm.schema import person, person_identity
from zerocrm.upsert import upsert_person

ACTOR = {"kind": "human", "id": "t"}


def _emails(engine) -> set[str]:
    with engine.connect() as c:
        rows = c.execute(
            select(person_identity.c.value).where(person_identity.c.kind == "email")
        ).scalars().all()
    return set(rows)


def test_happy_path_imports_new_contacts(engine):
    csv_text = (
        "name,email,phone\n"
        "Jane Doe,jane@acme.com,555-1000\n"
        "Bob Roe,bob@acme.com,555-2000\n"
    )
    result = import_contacts_csv(engine, csv_text, ACTOR)

    assert result == {"imported": 2, "skipped": 0, "errors": []}
    assert _emails(engine) == {"jane@acme.com", "bob@acme.com"}
    with engine.connect() as c:
        jane = c.execute(select(person).where(person.c.full_name == "Jane Doe")).mappings().one()
        phone = c.execute(
            select(person_identity.c.value).where(
                person_identity.c.person_id == jane["id"], person_identity.c.kind == "phone",
            )
        ).scalar_one()
    assert phone == "555-1000"


def test_malformed_rows_are_skipped_with_errors(engine):
    csv_text = (
        "name,email,phone\n"
        ",noname@acme.com,555-1000\n"          # missing name
        "No At Sign,not-an-email,555-2000\n"    # malformed email
        "Valid Row,valid@acme.com,555-3000\n"
    )
    result = import_contacts_csv(engine, csv_text, ACTOR)

    assert result["imported"] == 1
    assert result["skipped"] == 2
    assert any("row 2" in e and "missing name" in e for e in result["errors"])
    assert any("row 3" in e and "invalid email" in e for e in result["errors"])
    assert _emails(engine) == {"valid@acme.com"}


def test_duplicate_against_existing_contact_is_skipped(engine):
    with engine.begin() as conn:
        upsert_person(conn, identity={"kind": "email", "value": "jane@acme.com"},
                      fields={"full_name": "Jane Existing"}, provenance="human", actor=ACTOR)

    csv_text = "name,email,phone\nJane Doe,jane@acme.com,555-1000\n"
    result = import_contacts_csv(engine, csv_text, ACTOR)

    assert result == {"imported": 0, "skipped": 1,
                      "errors": ["row 2: duplicate email 'jane@acme.com'"]}
    with engine.connect() as c:
        count = c.execute(select(func.count()).select_from(person)).scalar_one()
    assert count == 1  # no second person row created


def test_duplicate_within_same_batch_is_skipped(engine):
    csv_text = (
        "name,email,phone\n"
        "Jane Doe,jane@acme.com,555-1000\n"
        "Jane Again,JANE@acme.com,555-9999\n"
    )
    result = import_contacts_csv(engine, csv_text, ACTOR)

    assert result["imported"] == 1
    assert result["skipped"] == 1
    assert result["errors"] == ["row 3: duplicate email 'jane@acme.com'"]
    with engine.connect() as c:
        count = c.execute(select(func.count()).select_from(person)).scalar_one()
    assert count == 1


def test_unique_constraint_race_is_skipped_not_fatal(engine, monkeypatch):
    # Simulate the check-then-insert race a concurrent request could hit: the
    # existing-email check reports "not found" (as it would for two requests
    # racing on the same new email), so the code reaches the actual INSERT,
    # which collides with the person_identity uq_identity unique constraint.
    # That must skip just this row, not abort the whole batch.
    with engine.begin() as conn:
        upsert_person(conn, identity={"kind": "email", "value": "jane@acme.com"},
                      fields={"full_name": "Existing"}, provenance="human", actor=ACTOR)
    monkeypatch.setattr("zerocrm.contacts_import.resolve_person", lambda *a, **k: None)
    monkeypatch.setattr("zerocrm.upsert.resolve_person", lambda *a, **k: None)

    csv_text = (
        "name,email,phone\n"
        "Jane Doe,jane@acme.com,555-1000\n"
        "Bob Roe,bob@acme.com,555-2000\n"
    )
    result = import_contacts_csv(engine, csv_text, ACTOR)

    assert result["imported"] == 1  # bob still imported despite jane's race loss
    assert result["skipped"] == 1
    assert any("duplicate" in e for e in result["errors"])
    with engine.connect() as c:
        count = c.execute(select(func.count()).select_from(person)).scalar_one()
    assert count == 2  # the pre-seeded jane + bob; no second jane row created
