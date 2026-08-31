"""Read-only contacts list: person + company + best email/phone, for the
/contacts page (see serve.py). No writes, no new tables."""

from __future__ import annotations

import html
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine import Engine

from .schema import company, person, person_identity

_CSV_JS = (Path(__file__).parent / "static" / "csv.js").read_text()

_COLUMNS = (("name", "Name"), ("email", "Email"), ("company", "Company"), ("phone", "Phone"))


def list_contacts(engine: Engine, workspace_id: str = "default") -> list[dict]:
    """Return [{name, email, company, phone}, ...] ordered by name."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(person.c.id, person.c.full_name, company.c.name.label("company_name"))
            .join(company, company.c.id == person.c.company_id, isouter=True)
            .where(person.c.workspace_id == workspace_id)
            .order_by(person.c.full_name)
        ).mappings().all()

        identities = conn.execute(
            select(person_identity.c.person_id, person_identity.c.kind, person_identity.c.value)
            .where(
                person_identity.c.workspace_id == workspace_id,
                person_identity.c.kind.in_(("email", "phone")),
                person_identity.c.retired_at.is_(None),
            )
        ).all()

    by_person: dict[str, dict[str, str]] = {}
    for person_id, kind, value in identities:
        by_person.setdefault(person_id, {}).setdefault(kind, value)

    contacts = []
    for row in rows:
        ident = by_person.get(row["id"], {})
        contacts.append({
            "name": row["full_name"] or "",
            "email": ident.get("email", ""),
            "company": row["company_name"] or "",
            "phone": ident.get("phone", ""),
        })
    return contacts


def render_contacts_page(contacts: list[dict]) -> bytes:
    """Contacts table with a client-side CSV export button. No backend
    export route: the button reads the rendered table and serializes it
    in the browser via static/csv.js."""
    head_cells = "".join(f"<th>{label}</th>" for _, label in _COLUMNS)
    body_rows = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(c.get(key, '')))}</td>" for key, _ in _COLUMNS) + "</tr>"
        for c in contacts
    )
    page = f"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width">
<title>Contacts</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 2rem; color: #1a1a1a; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 1rem; }}
  th, td {{ text-align: left; padding: 0.5rem 0.75rem; border-bottom: 1px solid #ddd; }}
  button#export-csv {{ padding: 0.5rem 1rem; font-size: 14px; cursor: pointer; }}
</style>
<h1>Contacts</h1>
<button id="export-csv" type="button">Export CSV</button>
<table id="contacts-table">
  <thead><tr>{head_cells}</tr></thead>
  <tbody>{body_rows}</tbody>
</table>
<script>
{_CSV_JS}
document.getElementById("export-csv").addEventListener("click", function () {{
  var table = document.getElementById("contacts-table");
  var columns = {json.dumps([{"key": key, "label": label} for key, label in _COLUMNS])};
  var rows = [];
  var trs = table.querySelectorAll("tbody tr");
  for (var i = 0; i < trs.length; i++) {{
    if (trs[i].offsetParent === null) continue; // skip hidden/filtered rows
    var cells = trs[i].querySelectorAll("td");
    var row = {{}};
    for (var j = 0; j < columns.length; j++) {{
      row[columns[j].key] = cells[j] ? cells[j].textContent : "";
    }}
    rows.push(row);
  }}
  var csv = toCsv(rows, columns);
  triggerDownload("contacts.csv", csv);
}});
</script>
"""
    return page.encode()
