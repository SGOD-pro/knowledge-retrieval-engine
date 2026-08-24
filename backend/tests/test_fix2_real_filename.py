import io
import tempfile
from pathlib import Path
import pytest
from schemas.models import Document
from ingestion.parse_service import parse_file, ingest_document

def test_real_filename_propagation_in_parse_file():
    # Create a temporary file with a random temporary name (e.g. tmp123abc.csv)
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        tmp.write(b"col1,col2\nval1,val2\n")
        tmp_path = Path(tmp.name)

    try:
        real_filename = "important_annual_report_2025.csv"
        
        # When filename is explicitly passed, Document.filename must match it
        doc = parse_file(tmp_path, filename=real_filename)
        assert doc.filename == real_filename
        assert doc.filename != tmp_path.name
        assert doc.id == str(pytest.importorskip("uuid").uuid5(pytest.importorskip("uuid").NAMESPACE_DNS, real_filename))
    finally:
        tmp_path.unlink(missing_ok=True)
