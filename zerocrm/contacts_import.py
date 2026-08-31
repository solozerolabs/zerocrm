"""CSV contact import — validates rows, dedupes by email against existing
person_identity records, and inserts new contacts through the precedence
gate (upsert_person).

There is no literal `contacts` table in this schema; a contact IS a `person`
identified by an email `person_identity` row (see upsert.py). Unlike the
normal upsert flow, a duplicate email here is a SKIP, not a merge: this
import only ever creates brand-new contacts.
"""

from __future__ import annotations

import csv
import io
import re

from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from .schema import person_identity
from .upsert import resolve_person, upsert_person

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def import_contacts_csv(engine: Engine, csv_text: str, actor: dict,
                        workspace_id: str = "default") -> dict:
    """Parse CSV text (header row: name,email,phone), skipping rows with a
    missing name or malformed/duplicate email, and insert the rest as new
    contacts. Row numbers in error messages count the header as row 1.

    Returns {"imported": n, "skipped": m, "errors": ["row N: ...", ...]}.
    """
    imported = skipped = 0
    errors: list[str] = []
    seen_emails: set[str] = set()

    reader = csv.DictReader(io.StringIO(csv_text))
    with engine.begin() as conn:
        for row_num, row in enumerate(reader, start=2):
            name = (row.get("name") or "").strip()
            email = (row.get("email") or "").strip().lower()

            if not name:
                skipped += 1
                errors.append(f"row {row_num}: missing name")
                continue
            if not email or not _EMAIL_RE.match(email):
                skipped += 1
                errors.append(f"row {row_num}: invalid email '{email}'")
                continue
            if email in seen_emails or resolve_person(conn, "email", email, workspace_id):
                skipped += 1
                errors.append(f"row {row_num}: duplicate email '{email}'")
                continue

            # A savepoint (not the outer transaction) guards the insert: two
            # concurrent imports can both pass the resolve_person check above
            # for the same email before either commits, so the loser's INSERT
            # can still hit the person_identity uq_identity unique constraint.
            # Without this, that IntegrityError would abort every row already
            # processed in this request, not just the colliding one.
            try:
                with conn.begin_nested():
                    person_id = upsert_person(
                        conn,
                        identity={"kind": "email", "value": email, "source": "csv_import"},
                        fields={"full_name": name},
                        provenance="imported",
                        actor=actor,
                        workspace_id=workspace_id,
                    )
                    phone = (row.get("phone") or "").strip()
                    if phone:
                        try:
                            with conn.begin_nested():
                                conn.execute(person_identity.insert().values(
                                    person_id=person_id, kind="phone", value=phone,
                                    source="csv_import", workspace_id=workspace_id, actor=actor,
                                ))
                        except IntegrityError:
                            pass  # phone already claimed by another identity; contact still imported
            except IntegrityError:
                skipped += 1
                errors.append(f"row {row_num}: duplicate email '{email}'")
                continue

            seen_emails.add(email)
            imported += 1

    return {"imported": imported, "skipped": skipped, "errors": errors}
