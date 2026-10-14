"""Administración autenticada de documentos y cotizaciones."""

import logging
import os
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

import app.integrations.documents as documents_client
import app.rag.indexing as rag
import app.schemas.quotes as quote_schema
from app.core import config, resources, security
from app.schemas import responses
from app.services import catalog

router = APIRouter()
logger = logging.getLogger("chatbot.api")


@router.post("/documents/upload", dependencies=[Depends(security.require_admin)])
async def upload_document(file: Annotated[UploadFile, File()]) -> dict:
    await resources.reserve("admin:documents", 10)
    filename = file.filename or ""
    if not filename or len(filename) > 200 or filename != Path(filename).name or ("\\" in filename):
        raise HTTPException(400, "Nombre de archivo inválido")
    suffix = Path(filename).suffix.lower()
    if suffix not in (".txt", ".pdf"):
        raise HTTPException(400, "Solo se aceptan archivos .txt o .pdf")
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = tmp.name
            size = 0
            while chunk := (await file.read(65536)):
                size += len(chunk)
                if size > 5 * 1024 * 1024:
                    raise HTTPException(413, "Archivo demasiado grande")
                tmp.write(chunk)
        chunks = await resources.rag_call(rag.ingest_document, tmp_path, source_name=filename)
    except (ValueError, RuntimeError):
        raise HTTPException(422, "No se pudo indexar el documento") from None
    finally:
        await file.close()
        if tmp_path:
            os.unlink(tmp_path)
    if not chunks:
        raise HTTPException(422, "No se pudo extraer texto")
    return {"filename": filename, "chunks_indexed": chunks}


@router.get("/documents", dependencies=[Depends(security.require_admin)])
async def list_documents() -> dict:
    return {"sources": await resources.rag_call(rag.list_sources)}


@router.delete("/documents/{source_name}", dependencies=[Depends(security.require_admin)])
async def delete_document(source_name: str) -> dict:
    deleted = await resources.rag_call(rag.delete_source, source_name)
    if not deleted:
        raise HTTPException(404, "Documento no encontrado")
    return {"deleted_chunks": deleted}


@router.post(
    "/documents/quote",
    response_model=responses.QuoteResponse,
    dependencies=[Depends(security.require_admin)],
)
async def create_quote(request: quote_schema.QuoteRequest) -> responses.QuoteResponse:
    await resources.reserve("admin:quotes", 10)
    if request.tenant_id != config.TENANT_ID:
        raise HTTPException(403, "Tenant no autorizado")
    try:
        draft = catalog.price_draft(
            {
                "intent": "quote",
                "customer": request.customer.model_dump(),
                "items": [i.model_dump() for i in request.items],
                "currency": request.currency,
            }
        )
    except ValueError:
        raise HTTPException(422, "El negocio debe confirmar el precio") from None
    result = await documents_client.create_quote(
        request_id=request.request_id,
        tenant_id=config.TENANT_ID,
        customer=draft["customer"],
        items=draft["items"],
        currency=draft["currency"],
    )
    if result is None:
        raise HTTPException(502, "No se pudo generar la cotización")
    return responses.QuoteResponse(**vars(result))
