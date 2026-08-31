from datetime import datetime, timezone

from sqlalchemy import insert

from zerocrm import contacts
from zerocrm.db import make_engine
from zerocrm.migrate import migrate
from zerocrm.schema import person, person_identity


def _engine():
    e = make_engine("sqlite://")
    migrate(e)
    return e


def test_list_contacts_returns_name_and_email():
    e = _engine()
    with e.begin() as c:
        c.execute(insert(person).values(id="p1", full_name="Jane Doe"))
        c.execute(insert(person_identity).values(
            person_id="p1", kind="email", value="jane@acme.com"))
    got = contacts.list_contacts(e)
    assert got == [{"id": "p1", "name": "Jane Doe", "email": "jane@acme.com"}]


def test_list_contacts_ignores_retired_identity_and_dedupes():
    e = _engine()
    with e.begin() as c:
        c.execute(insert(person).values(id="p1", full_name="Bob"))
        c.execute(insert(person_identity).values(
            person_id="p1", kind="email", value="old@bob.com",
            retired_at=datetime(2020, 1, 1, tzinfo=timezone.utc)))
        c.execute(insert(person_identity).values(
            person_id="p1", kind="email", value="bob@acme.com"))
    got = contacts.list_contacts(e)
    assert len(got) == 1
    assert got[0]["email"] == "bob@acme.com"


def test_list_contacts_picks_lowest_id_when_two_active_emails():
    e = _engine()
    with e.begin() as c:
        c.execute(insert(person).values(id="p1", full_name="Multi"))
        c.execute(insert(person_identity).values(
            id="i2", person_id="p1", kind="email", value="second@acme.com"))
        c.execute(insert(person_identity).values(
            id="i1", person_id="p1", kind="email", value="first@acme.com"))
    got = contacts.list_contacts(e)
    assert len(got) == 1
    assert got[0]["email"] == "first@acme.com"  # lowest identity id wins, deterministically


def test_list_contacts_blank_email_when_no_identity():
    e = _engine()
    with e.begin() as c:
        c.execute(insert(person).values(id="p1", full_name="No Email"))
    got = contacts.list_contacts(e)
    assert got == [{"id": "p1", "name": "No Email", "email": ""}]


def test_render_contacts_page_lists_rows_and_search_input():
    html = contacts.render_contacts_page(
        [{"id": "p1", "name": "Jane Doe", "email": "jane@acme.com"}])
    assert '<input id="contact-search"' in html
    assert "Jane Doe" in html and "jane@acme.com" in html
    assert 'data-name="jane doe"' in html and 'data-email="jane@acme.com"' in html
    # non-empty list starts with the empty-state message hidden
    assert 'id="contacts-empty" style="display:none;color:#666"' in html


def test_render_contacts_page_shows_empty_state_with_no_contacts():
    html = contacts.render_contacts_page([])
    assert '<input id="contact-search"' in html
    assert 'id="contacts-empty" style="color:#666"' in html
    assert "No contacts match your search." in html


def test_render_contacts_page_escapes_content():
    html = contacts.render_contacts_page(
        [{"id": "p1", "name": "<script>x</script>", "email": "a&b@x.com"}])
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;" in html and "a&amp;b@x.com" in html
