import pytest
from sqlalchemy import func, insert, select

from zerocrm import contracts, documents
from zerocrm.db import make_engine
from zerocrm.migrate import migrate
from zerocrm.schema import company, document, location, memory_chunk

A = {"kind": "human", "id": "sid"}


def _engine_cid():
    e = make_engine("sqlite://")
    migrate(e)
    with e.begin() as c:
        c.execute(insert(company).values(id="co1", name="Acme"))
        c.execute(insert(location).values(id="loc1", company_id="co1", name="HQ"))
    cid = contracts.create_contract(e, company_id="co1", location_id="loc1",
                                    scope="nightly janitorial", contract_type="jan", actor=A)
    return e, cid


def test_attach_with_text_chunks_into_memory():
    e, cid = _engine_cid()
    documents.attach_document(e, contract_id=cid, filename="confirm.txt",
                              text="signed confirmation of nightly service", actor=A)
    with e.connect() as c:
        assert c.execute(select(func.count()).select_from(memory_chunk)
                         .where(memory_chunk.c.source_type == "document")).scalar() >= 1
        assert c.execute(select(document.c.chunked)).scalar() == 1


def test_attach_without_text_does_not_chunk():
    e, cid = _engine_cid()
    documents.attach_document(e, contract_id=cid, filename="scan.pdf",
                              uri="s3://bucket/scan.pdf", actor=A)
    with e.connect() as c:
        assert c.execute(select(func.count()).select_from(memory_chunk)).scalar() == 0


def test_attach_requires_a_parent():
    e, _ = _engine_cid()
    with pytest.raises(ValueError):
        documents.attach_document(e, filename="orphan.txt", text="x", actor=A)


def test_generate_persists_rendered_body_and_event():
    e, cid = _engine_cid()
    did = documents.generate_document(e, contract_id=cid,
                                      template="Scope: $scope / Type: $contract_type", actor=A)
    with e.connect() as c:
        row = c.execute(select(document).where(document.c.id == did)).mappings().first()
    assert row["generated"] == 1
    assert row["body"] == "Scope: nightly janitorial / Type: jan"  # rendered text persisted
    assert any(h["kind"] == "doc_generated" for h in contracts.contract_history(e, cid))
