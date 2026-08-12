"""Docs-on-record: the record of truth for uploaded/generated files. A text doc
MAY also chunk into the slice-1 memory_chunk spine for retrieval; the record here
stays authoritative. Blob upload (file -> uri) is caller-supplied; the storage
driver is deferred. Generation is string.Template text/HTML — PDF is a later seam."""

from __future__ import annotations

from string import Template

from sqlalchemy import insert, select
from sqlalchemy.engine import Engine

from .contracts import _event
from .memory import add_chunks, chunk_text
from .schema import contract, document


def attach_document(engine: Engine, *, contract_id=None, location_id=None, filename,
                    uri=None, doc_type=None, text=None, actor,
                    workspace_id: str = "default") -> str:
    if not (contract_id or location_id):
        raise ValueError("a document must attach to a contract or a location")
    chunks = chunk_text(text) if (text and text.strip()) else []
    with engine.begin() as conn:
        did = conn.execute(insert(document).values(
            contract_id=contract_id, location_id=location_id, filename=filename,
            uri=uri, body=text, doc_type=doc_type, generated=False,
            chunked=bool(chunks), actor=actor,
            workspace_id=workspace_id)).inserted_primary_key[0]
        if chunks:  # atomic: record, chunks, and the chunked flag share one txn
            add_chunks(engine, source_type="document", source_id=did, texts=chunks,
                       workspace_id=workspace_id, conn=conn)
        if contract_id:
            _event(conn, contract_id, "doc_attached", {"filename": filename}, actor, workspace_id)
    return did


def generate_document(engine: Engine, *, contract_id, template, filename="contract.md",
                      actor, workspace_id: str = "default") -> str:
    with engine.begin() as conn:
        row = conn.execute(select(contract).where(contract.c.id == contract_id)).mappings().first()
        fields = {k: ("" if v is None else v) for k, v in dict(row or {}).items()}
        rendered = Template(template).safe_substitute(fields)
        did = conn.execute(insert(document).values(
            contract_id=contract_id, filename=filename, doc_type="generated",
            body=rendered, generated=True, chunked=False, actor=actor,
            workspace_id=workspace_id)).inserted_primary_key[0]
        _event(conn, contract_id, "doc_generated",
               {"filename": filename, "length": len(rendered)}, actor, workspace_id)
    return did
