"""Read-only contacts list: person + primary email identity, rendered as a
searchable HTML page. No framework (matches serve.py) — filtering runs
client-side in a small inline script against the already-rendered rows.
"""

from __future__ import annotations

import html as _html

from sqlalchemy import select
from sqlalchemy.engine import Engine

from .schema import person, person_identity


def list_contacts(engine: Engine) -> list[dict]:
    """Every person with their active email identity (if any), name-sorted."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(person.c.id, person.c.full_name, person_identity.c.value.label("email"))
            .select_from(person)
            .outerjoin(
                person_identity,
                (person_identity.c.person_id == person.c.id)
                & (person_identity.c.kind == "email")
                & (person_identity.c.retired_at.is_(None)),
            )
            .order_by(person.c.full_name, person_identity.c.id)
        ).mappings().all()
    seen: set[str] = set()
    contacts = []
    for r in rows:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        contacts.append({"id": r["id"], "name": r["full_name"] or "", "email": r["email"] or ""})
    return contacts


def render_contacts_page(contacts: list[dict]) -> str:
    e = _html.escape
    rows = "".join(
        f'<tr class="contact-row" data-name="{e(c["name"].lower())}" data-email="{e(c["email"].lower())}">'
        f'<td>{e(c["name"])}</td><td>{e(c["email"])}</td></tr>'
        for c in contacts
    )
    # initial contacts-empty visibility mirrors "no rows at all"; the script
    # below re-derives it on every keystroke against the search query.
    empty_style = "display:none;color:#666" if contacts else "color:#666"
    return f'''<!doctype html>
<meta name="viewport" content="width=device-width">
<title>Contacts</title>
<style>
body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:720px;margin:40px auto;color:#1a1a1a}}
table{{width:100%;border-collapse:collapse}}
td,th{{text-align:left;padding:6px 8px;border-bottom:1px solid #eee}}
input{{width:100%;box-sizing:border-box;padding:8px;margin-bottom:12px;font-size:15px}}
</style>
<h1>Contacts</h1>
<input id="contact-search" type="text" placeholder="Search by name or email" autocomplete="off">
<table id="contacts-table">
<thead><tr><th>Name</th><th>Email</th></tr></thead>
<tbody>{rows}</tbody>
</table>
<p id="contacts-empty" style="{empty_style}">No contacts match your search.</p>
<script>
(function () {{
  var input = document.getElementById("contact-search");
  var rows = Array.prototype.slice.call(document.querySelectorAll(".contact-row"));
  var empty = document.getElementById("contacts-empty");
  input.addEventListener("input", function () {{
    var q = input.value.trim().toLowerCase();
    var visible = 0;
    rows.forEach(function (row) {{
      var match = row.dataset.name.indexOf(q) !== -1 || row.dataset.email.indexOf(q) !== -1;
      row.style.display = match ? "" : "none";
      if (match) visible++;
    }});
    empty.style.display = visible === 0 ? "" : "none";
  }});
}})();
</script>
'''
