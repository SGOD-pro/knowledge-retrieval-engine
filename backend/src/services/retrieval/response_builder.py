"""Response builder for the Query Lambda retrieval pipeline.

Constructs Citation and FastPathResponse objects from scored Chunk results.

Rule 20: Every citation must carry a non-null location reference.
Image URLs: Generated at query time via presigned S3 URLs — NEVER at ingest
time — because presigned URLs expire.  No public ACLs are used.
"""

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

from aws.infra import get_client
from config import settings
from schemas.models import Chunk


def get_signed_image_url(key: str, bucket: str) -> str:
    s3 = get_client("s3")
    return s3.generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=3600
    )


# Bucket used when the document record does not carry an explicit per-doc bucket.
_DEFAULT_IMAGE_BUCKET = settings.S3_BUCKET_NAME


@dataclass(frozen=True)
class Citation:
    chunk_id: str
    document_id: str
    source_format: str
    bounding_box: dict[str, float] | None
    location_reference: str | None
    text_snippet: str
    text: str = ""
    document_filename: str = ""
    document_version: str | None = None
    page_number: int | None = None
    strategy: str = ""
    # Presigned S3 URLs for images co-extracted with this chunk.
    # Generated fresh per query so they are never stale.
    image_urls: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Serialise tuple as plain list for JSON consumers.
        d["image_urls"] = list(self.image_urls)
        return d


@dataclass(frozen=True)
class FastPathResponse:
    answer: str
    citations: list[Citation]
    confidence_score: float
    confidence_band: str
    fast_path: bool
    retrieval_path: list[str]
    latency_breakdown: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["citations"] = [c.to_dict() for c in self.citations]
        return d


_DOC_FILENAME_CACHE: dict[str, str] = {}


def resolve_chunk_provenance(
    chunk: Any,
    workspace_id: str | None = None,
    boundary_repo: Any | None = None,
    document_filename: str | None = None,
    document_version: str | None = None,
) -> tuple[str, str | None]:
    """Authoritatively resolve canonical document filename and version for a chunk.

    Enforces workspace boundary authority, tombstone checks, and document version bindings:
      1. Wrong-workspace binding: If the chunk explicitly belongs to a different workspace
         than the request-authorized workspace, it must NEVER borrow a filename or version
         from the request workspace.
      2. Tombstone checks: Tombstoned workspaces or sources must never certify active snapshots.
      3. Version mismatch: If the chunk carries an explicit version that disagrees with the
         authoritative snapshot version, it must NEVER be relabeled as the current version or
         certified with the snapshot's canonical filename.
      4. Canonical resolution: When verified or unversioned in an active snapshot, returns
         the authoritative snapshot source filename and version.
      5. Fallbacks: Falls back to chunk metadata, document_filename override, cache, or
         Doc-{id[:8]} identifiers without fabricating version certification.
    """
    chunk_meta = getattr(chunk, "metadata", None)
    if not isinstance(chunk_meta, dict):
        chunk_meta = {}

    # Extract chunk identities
    c_ws = getattr(chunk, "workspace_id", None) or chunk_meta.get("workspace_id")
    c_did = str(getattr(chunk, "document_id", "") or chunk_meta.get("document_id") or "")
    c_ver = (
        getattr(chunk, "document_version", None)
        or getattr(chunk, "version", None)
        or chunk_meta.get("document_version")
        or chunk_meta.get("version")
    )

    meta_fname = chunk_meta.get("filename") or chunk_meta.get("source")
    fallback_fname = (
        document_filename
        or getattr(chunk, "document_filename", None)
        or meta_fname
        or (f"Doc-{c_did[:8]}" if c_did else "Doc-unknown")
    )
    fallback_ver = (
        document_version
        if document_version is not None
        else (str(c_ver).strip() if c_ver is not None and str(c_ver).strip() else None)
    )

    # 1. Reject cross-workspace borrowing (wrong-workspace binding)
    if c_ws and workspace_id and str(c_ws).strip() != str(workspace_id).strip():
        return fallback_fname, fallback_ver

    auth_ws = str(workspace_id).strip() if workspace_id else (str(c_ws).strip() if c_ws else "")

    # Resolve boundary repository if omitted
    if boundary_repo is None and auth_ws:
        try:
            from db.workspace_boundary_repo import get_workspace_boundary_repo
            boundary_repo = get_workspace_boundary_repo()
        except Exception:
            boundary_repo = None

    # 2. Preserve existing tombstone checks
    if boundary_repo is not None and auth_ws and hasattr(boundary_repo, "has_tombstone"):
        try:
            if boundary_repo.has_tombstone(auth_ws, "workspace", auth_ws):
                return fallback_fname, fallback_ver
            if c_did and boundary_repo.has_tombstone(auth_ws, "source", c_did):
                return fallback_fname, fallback_ver
        except Exception:
            return fallback_fname, fallback_ver

    # 3. Lookup authoritative document snapshot
    snapshot = None
    if boundary_repo is not None and auth_ws and c_did and hasattr(boundary_repo, "get_document_snapshot"):
        try:
            snapshot = boundary_repo.get_document_snapshot(auth_ws, c_did)
        except Exception:
            snapshot = None

    if snapshot is not None:
        snap_source = getattr(snapshot, "source", None)
        snap_ver = getattr(snapshot, "version", None)
        snap_ver_str = str(snap_ver).strip() if snap_ver is not None else None

        # Check declared version against snapshot version
        cand_ver = str(c_ver).strip() if c_ver is not None and str(c_ver).strip() else None
        if document_version is not None and str(document_version).strip():
            cand_ver = str(document_version).strip()

        if cand_ver is not None and snap_ver_str is not None:
            if cand_ver != snap_ver_str:
                # Version mismatch: NEVER relabel unverified chunk as current version
                return fallback_fname, cand_ver
            # Declared version verified against snapshot
            return snap_source or fallback_fname, snap_ver_str

        # Chunk is unversioned (e.g. tabular/XLS) or snapshot has version
        resolved_fname = snap_source or fallback_fname
        resolved_ver = snap_ver_str or fallback_ver
        return resolved_fname, resolved_ver

    # 4. Fallback to CloudRepository / cache if no snapshot found
    doc_fname = document_filename or getattr(chunk, "document_filename", "") or ""
    if not doc_fname and c_did in _DOC_FILENAME_CACHE:
        doc_fname = _DOC_FILENAME_CACHE[c_did]
    elif not doc_fname and c_did:
        try:
            from db.database import CloudRepository
            doc = CloudRepository().get(c_did)
            if doc and doc.filename:
                doc_fname = doc.filename
                _DOC_FILENAME_CACHE[c_did] = doc_fname
        except Exception:
            pass

    if not doc_fname:
        doc_fname = fallback_fname

    return doc_fname, fallback_ver


def build_citation(
    chunk: Chunk,
    document_filename: str | None = None,
    image_bucket: str | None = None,
    workspace_id: str | None = None,
    boundary_repo: Any | None = None,
    document_version: str | None = None,
) -> Citation:
    """Build a citation enforcing Rule 20 (zero null location responses).

    Args:
        chunk:             The source Chunk from the retrieval pipeline.
        document_filename: Optional document filename to include in citation.
        image_bucket:      S3 bucket that holds the chunk's images. Falls back to
                           the ``S3_BUCKET_NAME`` env var then a dev default.
        workspace_id:      Request-authorized workspace ID for boundary validation.
        boundary_repo:     Authoritative boundary repository for document snapshots.
        document_version:  Optional document version override.

    Returns:
        A fully populated Citation with fresh presigned image URLs (if any).
    """
    bbox = chunk.bounding_box
    loc_ref = chunk.location_reference

    # Rule 20 fallback check
    if chunk.source_format == "pdf":
        if bbox is None:
            bbox = {
                "x1": 0.0,
                "y1": 0.0,
                "x2": 1.0,
                "y2": 1.0,
                "page_number": chunk.page_number or 1,
            }
    else:
        if not loc_ref:
            if chunk.element_type == "heading":
                loc_ref = f"Heading: {chunk.text[:30]}"
            elif chunk.page_number:
                loc_ref = f"Page: {chunk.page_number}"
            else:
                loc_ref = f"Section: {chunk.element_type}"

    # Determine page number
    page_num = chunk.page_number
    if page_num is None and isinstance(bbox, dict) and "page_number" in bbox:
        try:
            page_num = int(bbox["page_number"])
        except (ValueError, TypeError):
            pass

    # Resolve document filename and document version authoritatively
    doc_fname, doc_ver = resolve_chunk_provenance(
        chunk=chunk,
        workspace_id=workspace_id,
        boundary_repo=boundary_repo,
        document_filename=document_filename,
        document_version=document_version,
    )

    # Generate presigned URLs at query time — URLs expire, must be fresh per response.
    # Security: no public ACLs, no bucket policy changes. Presigned only.
    image_urls: tuple[str, ...] = ()
    if chunk.image_s3_keys:
        bucket = image_bucket or _DEFAULT_IMAGE_BUCKET
        urls = []
        for key in chunk.image_s3_keys:
            try:
                urls.append(get_signed_image_url(key, bucket))
            except Exception:
                # Never let a presign failure break query responses — skip the key.
                pass
        image_urls = tuple(urls)

    strat = (
        getattr(chunk, "strategy", "")
        or ((chunk.metadata or {}).get("strategy") if isinstance(getattr(chunk, "metadata", None), dict) else "")
        or "vector_rerank"
    )

    return Citation(
        chunk_id=chunk.id,
        document_id=str(chunk.document_id),
        document_filename=doc_fname,
        document_version=doc_ver,
        source_format=chunk.source_format,
        page_number=page_num,
        bounding_box=bbox,
        location_reference=loc_ref,
        text=chunk.text[:500],
        text_snippet=chunk.text[:200],
        strategy=strat,
        image_urls=image_urls,
    )


def build_fast_path_response(
    query: str,
    scored_chunks: Sequence[tuple[Chunk, float]],
    latency_breakdown: dict[str, float],
    workspace_id: str | None = None,
    boundary_repo: Any | None = None,
) -> FastPathResponse:
    if not scored_chunks:
        return FastPathResponse(
            answer="NOT_FOUND: No relevant documents found matching query.",
            citations=[],
            confidence_score=0.0,
            confidence_band="LOW",
            fast_path=True,
            retrieval_path=["bm25", "page_index", "vector"],
            latency_breakdown=latency_breakdown,
        )

    top_chunks = [c for c, _ in scored_chunks[:3]]
    citations = [
        build_citation(c, workspace_id=workspace_id, boundary_repo=boundary_repo)
        for c in top_chunks
    ]

    # Combine top chunk texts into factual answer summary without LLM call (Rule 1)
    combined_answers = " ".join([c.text for c in top_chunks])
    answer = combined_answers[:500]

    vector_similarity_avg = sum(s for _, s in scored_chunks[:3]) / max(
        1, len(top_chunks)
    )
    # Coverage ratio estimate
    query_words = set(query.lower().split())
    answer_words = set(answer.lower().split())
    coverage_ratio = len(query_words.intersection(answer_words)) / max(
        1, len(query_words)
    )

    confidence = min(
        1.0, max(0.0, (vector_similarity_avg * 0.6) + (coverage_ratio * 0.4))
    )

    if confidence >= 0.75:
        band = "HIGH"
    elif confidence >= 0.50:
        band = "MEDIUM"
    else:
        band = "LOW"

    return FastPathResponse(
        answer=answer,
        citations=citations,
        confidence_score=round(confidence, 4),
        confidence_band=band,
        fast_path=True,
        retrieval_path=["bm25", "page_index", "vector"],
        latency_breakdown=latency_breakdown,
    )
