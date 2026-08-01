"""Runnable check for two non-trivial odl/main.py behaviors.

Tests:
  1. Partial failure: convert() raises on a corrupt file but still
     writes outputs for the valid files processed before the failure.
  2. Naming convention: outputs are flat {basename}.md / {basename}.json
     in out_dir, and images go into {basename}_images/ subdirectories.

Run with the opendataloader-pdf venv:
  /tmp/venv2/bin/python test_odl.py
"""
import glob
import shutil
import sys
from pathlib import Path

try:
    import opendataloader_pdf
except ImportError:
    print("SKIP: opendataloader_pdf not available in this environment.")
    sys.exit(0)

# ── Setup ────────────────────────────────────────────────────────────────────
tmp_dir = Path("/tmp/odl_test_check")
if tmp_dir.exists():
    shutil.rmtree(tmp_dir)
inputs_dir = tmp_dir / "inputs"
out_dir = tmp_dir / "out"
inputs_dir.mkdir(parents=True)
out_dir.mkdir(parents=True)

real_pdfs = glob.glob("/mnt/d/knowledge-retrieval-engine/data/academic_research/*.pdf")[:3]
assert real_pdfs, "No real PDFs found — update the glob path."

input_paths = []
doc_ids = []
for i, pdf in enumerate(real_pdfs):
    doc_id = f"valid_{i}"
    dest = inputs_dir / f"{doc_id}.pdf"
    shutil.copy(pdf, dest)
    input_paths.append(str(dest))
    doc_ids.append(doc_id)

corrupt_id = "corrupt_1"
corrupt_dest = inputs_dir / f"{corrupt_id}.pdf"
corrupt_dest.write_text("this is not a pdf file")
input_paths.append(str(corrupt_dest))

# ── Run ──────────────────────────────────────────────────────────────────────
raised = False
try:
    opendataloader_pdf.convert(
        input_path=input_paths,
        output_dir=str(out_dir),
        format="json,markdown",
        image_output="external",
        image_dir=str(out_dir),
    )
except Exception as e:
    raised = True
    print(f"convert() raised (expected): {type(e).__name__}")

# ── Assertions ───────────────────────────────────────────────────────────────
assert raised, "FAIL: expected convert() to raise on corrupt file, it did not"
print("PASS: convert() raises CalledProcessError on corrupt file")

for doc_id in doc_ids:
    md = out_dir / f"{doc_id}.md"
    js = out_dir / f"{doc_id}.json"
    assert md.exists() or js.exists(), f"FAIL: no output for valid doc {doc_id}"
print(f"PASS: all {len(doc_ids)} valid docs produced output despite the corrupt file")

corrupt_md = out_dir / f"{corrupt_id}.md"
corrupt_json = out_dir / f"{corrupt_id}.json"
assert not corrupt_md.exists() and not corrupt_json.exists(), \
    "FAIL: corrupt doc unexpectedly produced output"
print("PASS: corrupt doc produced no output (will be detected as failed by lambda)")

# Confirm image dir convention: {doc_id}_images/ not flat files
for doc_id in doc_ids:
    img_dir = out_dir / f"{doc_id}_images"
    if img_dir.exists():
        images = list(img_dir.iterdir())
        print(f"PASS: images for {doc_id} in {img_dir.name}/ ({len(images)} files)")
        flat_matches = [f for f in out_dir.iterdir()
                        if f.is_file() and f.name.startswith(doc_id)
                        and f.suffix.lower() in ('.png', '.jpg', '.jpeg')]
        assert not flat_matches, \
            f"FAIL: unexpected flat image files for {doc_id}: {flat_matches}"

shutil.rmtree(tmp_dir)
print("\nAll checks passed.")
