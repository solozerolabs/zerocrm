from datetime import datetime, timezone

from sqlalchemy import insert

from zerocrm.contacts import list_contacts, render_contacts_page
from zerocrm.schema import company, person, person_identity


def _engine(engine):
    with engine.begin() as c:
        c.execute(insert(company).values(id="co1", name="Acme"))
        c.execute(insert(person).values(id="p1", full_name="Jane Doe", company_id="co1"))
        c.execute(insert(person).values(id="p2", full_name="Ann Smith"))
        c.execute(insert(person_identity).values(
            id="i1", person_id="p1", kind="email", value="jane@acme.com"))
        c.execute(insert(person_identity).values(
            id="i2", person_id="p1", kind="phone", value="555-1234"))
        # retired identity must not be returned
        c.execute(insert(person_identity).values(
            id="i3", person_id="p1", kind="email", value="old@acme.com",
            retired_at=datetime(2020, 1, 1, tzinfo=timezone.utc)))
    return engine


def test_list_contacts_joins_company_and_identity(engine):
    e = _engine(engine)
    contacts = list_contacts(e)
    by_name = {c["name"]: c for c in contacts}
    assert by_name["Jane Doe"] == {
        "name": "Jane Doe", "email": "jane@acme.com", "company": "Acme", "phone": "555-1234",
    }
    assert by_name["Ann Smith"] == {"name": "Ann Smith", "email": "", "company": "", "phone": ""}


def test_render_contacts_page_has_export_button_and_serializer(engine):
    e = _engine(engine)
    page = render_contacts_page(list_contacts(e)).decode()
    assert '<button id="export-csv"' in page
    assert "function toCsv(" in page
    assert "Jane Doe" in page and "jane@acme.com" in page and "Acme" in page and "555-1234" in page


def test_render_contacts_page_escapes_html(engine):
    page = render_contacts_page([
        {"name": "<script>alert(1)</script>", "email": "a@b.com", "company": "Acme", "phone": ""}
    ]).decode()
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page
