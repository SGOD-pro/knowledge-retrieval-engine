import tempfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile

from kre.db.postgres import PostgresRepository
from kre.ingestion.parse_service import parse_file

app = FastAPI(title="Knowledge Retrieval Engine")


def repository() -> PostgresRepository:
    return PostgresRepository()


@app.post("/ingest")
async def ingest(file: UploadFile = File(...)):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".pdf", ".docx", ".xlsx", ".pptx"}:
        raise HTTPException(415, "Supported formats: PDF, DOCX, XLSX, PPTX")
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temporary:
        temporary.write(await file.read())
        path = Path(temporary.name)
    try:
        document = parse_file(path)
        repository().save(document)
        return {"id": document.id, "filename": document.filename, "source_format": document.source_format, "chunk_count": len(document.chunks)}
    finally:
        path.unlink(missing_ok=True)


@app.get("/documents/{document_id}")
def get_document(document_id: str):
    document = repository().get(document_id)
    if document is None:
        raise HTTPException(404, "Document not found")
    return document.to_dict()
