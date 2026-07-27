import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from kre.models import Chunk


def parse(path: Path, document_id: str, executable: str = "opendataloader-pdf") -> list[Chunk]:
    """Run opendataloader-pdf in batch mode and normalize its JSON output.

    Keeping the subprocess boundary here prevents parser-specific details from
    leaking into the unified ingestion service.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        subprocess.run(
            [executable, "-q", "-f", "json", "-o", tmp_dir, str(path)],
            check=True, capture_output=True, text=True,
        )
        json_file = Path(tmp_dir) / f"{path.stem}.json"
        if not json_file.exists():
            json_files = list(Path(tmp_dir).glob("*.json"))
            if json_files:
                json_file = json_files[0]
            else:
                return []
        payload: Any = json.loads(json_file.read_text(encoding="utf-8"))

    items = payload if isinstance(payload, list) else (payload.get("kids") or payload.get("pages") or [])
    chunks: list[Chunk] = []

    for index, element in enumerate(items):
        if not isinstance(element, dict):
            continue
        text = str(element.get("content") or element.get("source") or element.get("text") or "").strip()
        if not text or (element.get("type") == "image" and text.endswith((".png", ".jpg", ".jpeg"))):
            continue

        page_number = int(element.get("page number") or element.get("page_number") or element.get("page") or 1)
        raw_box = element.get("bounding box") or element.get("bounding_box") or element.get("bbox")
        bounding_box = None
        if isinstance(raw_box, (list, tuple)) and len(raw_box) == 4:
            bounding_box = {"x1": float(raw_box[0]), "y1": float(raw_box[1]), "x2": float(raw_box[2]), "y2": float(raw_box[3])}
        elif isinstance(raw_box, dict):
            bounding_box = {k: float(v) for k, v in raw_box.items()}

        element_type = str(element.get("type") or element.get("element_type") or "paragraph")

        chunks.append(Chunk(
            id=f"{document_id}:page:{page_number}:element:{index}",
            document_id=document_id,
            source_format="pdf",
            text=text,
            element_type=element_type,
            page_number=page_number,
            bounding_box=bounding_box,
            location_reference=f"Page: {page_number}",
        ))

    return chunks

