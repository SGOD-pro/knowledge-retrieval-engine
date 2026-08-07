# KRE API Contract

This document defines the strict API contract between the Frontend and the Query Lambda backend. It outlines the required requests, expected responses, and the detailed schema for citations.

---

## Citation Schema

The `Citation` object is returned as part of the `/query` response and is crucial for rendering the Document Viewer and Citations list in the right pane.

**Model:**
```json
{
  "id": "string",
  "document_id": "string",
  "source_format": "string", 
  "snippet": "string",
  "location_reference": "string",
  "bounding_box": [0, 0, 0, 0] // [left, top, right, bottom] - Optional
}
```

### Parsing Rules for Frontend:
- **PDF (`.pdf`)**: 
  - The `bounding_box` will be an array of 4 coordinates `[l, t, r, b]`. 
  - Use these coordinates to render a warm coral (`#cc785c`) transparent highlight overlay on top of `react-pdf`.
  - The `location_reference` will typically hold the page number (e.g., "Page 14").
- **DOCX / XLSX / PPTX (`.docx`, `.xlsx`, `.pptx`)**:
  - The `bounding_box` will be `null` or omitted.
  - The frontend should render a clean card displaying the `location_reference` string (e.g., "Sheet: Revenue, Row: 14" or "Section 4.1").
  - The `snippet` should be displayed to give context.

---

## Endpoints

### 1. `POST /query`
**Description:** Executes a multi-agent retrieval pipeline to answer a user's query. Used by the frontend when a user types a query in the center pane and clicks "Send".

**Request Body (`QueryRequest`):**
```json
{
  "query": "string (Required) - The user's question.",
  "doc_ids": ["string"] /* (Optional) - Array of document IDs to restrict the search. */,
  "path_override": "string" /* (Optional) - Forces a specific retrieval path if needed. */
}
```

**Response Body (`QueryResponse`):**
```json
{
  "answer": "string - The LLM generated response.",
  "citations": [
    {
      "id": "string",
      "document_id": "string",
      "source_format": "string",
      "snippet": "string",
      "location_reference": "string",
      "bounding_box": [0, 0, 0, 0]
    }
  ],
  "retrieval_path": ["string"], // e.g., ["BM25", "Vector", "LLM"]
  "confidence": 0.0, // float
  "latency_ms": 0 // integer
}
```

---

### 2. `GET /documents`
**Description:** Retrieves a list of all documents available in the knowledge graph. The frontend uses this to populate the document library or a sidebar.

**Request Body:** None.

**Response Body:**
```json
[
  {
    "id": "string",
    "filename": "string",
    "source_format": "string",
    "status": "string", // e.g., "Ingested", "Processing"
    "created_at": "string (ISO 8601)"
  }
]
```

---

### 3. `POST /documents`
**Description:** Creates a new document record in the database before uploading the actual file.

**Request Body:**
```json
{
  "filename": "string (Required)"
}
```

**Response Body:**
```json
{
  "id": "string - The newly created document ID.",
  "filename": "string",
  "status": "string" // Typically "Pending"
}
```

---

### 4. `POST /documents/{id}/ingest`
**Description:** Uploads the actual file for a previously created document record. This triggers parsing, chunking, and embedding generation.

**Path Parameters:**
- `id` (string) - The Document ID.

**Request:**
- **Content-Type:** `multipart/form-data`
- **Body:** `file` (Binary)

**Response Body:**
```json
{
  "id": "string",
  "filename": "string",
  "source_format": "string",
  "chunk_count": 0,
  "status": "string" // e.g., "Ingested"
}
```
