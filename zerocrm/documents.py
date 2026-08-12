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
from .storage import from_env as _storage_from_env


def attach_document(engine: Engine, *, contract_id=None, location_id=None, filename,
                    uri=None, doc_type=None, text=None, data=None, actor,
                    workspace_id: str = "default") -> str:
    """Record a document. Pass `text` to also chunk it for retrieval; pass `data`
    (bytes) to upload the blob to configured S3/R2 storage and use that as the uri."""
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
    # blob upload is network I/O -> outside the txn; the doc row already exists
    if data is not None:
        store = _storage_from_env()
        if store is None:
            raise RuntimeError("data given but blob storage is not configured (ZEROCRM_S3_*)")
        blob_uri = store.put(f"documents/{did}/{filename}", data,
                             content_type=doc_type or "application/octet-stream")
        with engine.begin() as conn:
            conn.execute(document.update().where(document.c.id == did).values(uri=blob_uri))
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
