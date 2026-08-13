# API Contract (rev 6)

All endpoints require `Authorization: Bearer <token>` unless explicitly stated (e.g., `/auth/login`).
Base URL: `/api/v1`

---

## 1. Authentication (Auth UI)

### POST /auth/login
Authenticates a user and returns a JWT access token.
- **Request Body:**
  ```json
  {
    "email": "user@enterprise.com",
    "password": "securepassword123",
    "remember_me": true
  }
  ```
- **Response 200 (OK):**
  ```json
  {
    "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
    "token_type": "bearer",
    "expires_in": 3600,
    "user": {
      "id": "usr_54321",
      "email": "user@enterprise.com",
      "name": "John Doe",
      "role": "analyst"
    }
  }
  ```

### GET /auth/oauth/{provider}
Redirects to OAuth provider (Google, Microsoft).
- **Path Params:** `provider` (string, required - `google` | `microsoft`)

---

## 2. Workspace Management (Workspace UI)

### GET /workspaces
Retrieves a list of workspaces accessible to the user.
- **Response 200 (OK):**
  ```json
  {
    "workspaces": [
      {
        "id": "ws_001",
        "name": "Finance Docs",
        "document_count": 142,
        "last_active": "2 hours ago",
        "status": "active"
      },
      {
        "id": "ws_002",
        "name": "Legal Contracts",
        "document_count": 56,
        "last_active": "1 day ago",
        "status": "active"
      }
    ]
  }
  ```

### POST /workspaces
Creates a new workspace.
- **Request Body:**
  ```json
  {
    "name": "Engineering R&D",
    "industry": "Technology",
    "description": "Q3 financial reports, clinical trial data, architecture design docs"
  }
  ```
- **Response 201 (Created):**
  ```json
  {
    "id": "ws_003",
    "name": "Engineering R&D",
    "industry": "Technology",
    "description": "Q3 financial reports, clinical trial data, architecture design docs",
    "document_count": 0,
    "created_at": "2026-08-11T10:00:00Z"
  }
  ```

---

## 3. Document Ingestion & Library (Upload & Library UI)

### POST /workspaces/{workspace_id}/documents
Uploads files for ingestion. Supports multipart/form-data.
- **Path Params:** `workspace_id` (string, required)
- **Request Body (multipart/form-data):**
  - `files`: (File[], required) - Array of files to upload (.pdf, .docx, .csv, .pptx)
- **Response 201 (Accepted):**
  ```json
  {
    "uploaded_documents": [
      {
        "id": "doc_uuid_1",
        "filename": "Senior_Dev_Resume_John_Doe.pdf",
        "format": "pdf",
        "status": "processing"
      },
      {
        "id": "doc_uuid_2",
        "filename": "candidate_dataset.csv",
        "format": "csv",
        "status": "processing"
      }
    ]
  }
  ```

### GET /workspaces/{workspace_id}/documents
Retrieves the document library for a workspace (with pagination).
- **Query Params:** `page=1`, `limit=10`
- **Response 200 (OK):**
  ```json
  {
    "documents": [
      {
        "id": "doc_uuid_1",
        "filename": "Senior_Dev_Resume_John_Doe.pdf",
        "format": "pdf",
        "upload_date": "Oct 24, 2023",
        "chunk_count": 12,
        "status": "Ready"
      },
      {
        "id": "doc_uuid_2",
        "filename": "Corrupted_File_Upload.pdf",
        "format": "pdf",
        "upload_date": "Oct 18, 2023",
        "chunk_count": 0,
        "status": "Failed"
      }
    ],
    "total_documents": 142,
    "current_page": 1,
    "total_pages": 15
  }
  ```

---

## 4. Query Pipeline (3-Pane Chat UI)

### POST /query
The core retrieval endpoint. The frontend sends a query, the backend routes through the LangGraph pipeline, and returns the answer with traceable citations.
- **Request Body:**
  ```json
  {
    "workspace_id": "ws_001",
    "query": "Summarize Candidate A's machine learning experience.",
    "document_ids": ["doc_uuid_1", "doc_uuid_2"] 
  }
  ```
- **Response 200 (OK):**
  ```json
  {
    "answer": "Candidate A has 4 years of machine learning experience [1]. They migrated the recommendation engine at TechFlow Inc. and deployed 3 predictive models in production [2].",
    "citations": [
      {
        "id": 1,
        "chunk_id": "chunk_uuid_1",
        "document_id": "doc_uuid_1",
        "document_filename": "Candidate_A_Resume_Final.pdf",
        "source_format": "pdf",
        "text": "Candidate A has 4 years of experience in machine learning, specializing in NLP and predictive modeling.",
        "page_number": 1,
        "bounding_box": { "l": 105.2, "t": 210.5, "r": 450.1, "b": 280.3 },
        "location_reference": "Page 1, Para 1"
      },
      {
        "id": 2,
        "chunk_id": "chunk_uuid_2",
        "document_id": "doc_uuid_2",
        "document_filename": "candidate_dataset.csv",
        "source_format": "csv",
        "text": "TechFlow Inc, Migrated recommendation engine, deployed 3 predictive models",
        "page_number": null,
        "bounding_box": null,
        "location_reference": "Sheet: Experience, Row: 14"
      }
    ],
    "retrieval_path": "full",
    "confidence": 0.92,
    "latency_ms": 1820
  }
  ```

---

## 5. Document Viewer

### GET /documents/{document_id}/file
Streams the raw file for rendering in the frontend viewer (e.g., PDF rendering).
- **Response 200 (OK):** `application/pdf` (or appropriate mime type) stream.

---

## 6. System Benchmarks (System Benchmarks UI)

### GET /system/benchmarks
Retrieves live system metrics to populate the dashboard.
- **Response 200 (OK):**
  ```json
  {
    "status": "FAILING TARGETS",
    "version": "v2.4.1",
    "kpis": {
      "p95_latency": {
        "value": 1800,
        "unit": "ms",
        "target": 1400,
        "delta": "+0.4s vs target",
        "status": "failing"
      },
      "recall_5": {
        "value": 94,
        "unit": "%",
        "target": 85,
        "delta": "+2% vs target",
        "status": "passing"
      },
      "faithfulness": {
        "value": 98,
        "unit": "%",
        "target": 100,
        "delta": "On target",
        "status": "passing"
      },
      "llm_activation": {
        "value": 12,
        "unit": "%",
        "target": 15,
        "delta": "-3% vs baseline",
        "status": "passing"
      }
    },
    "latency_chart": {
      "target_line": 1400,
      "data_points": [
        { "timestamp": "Mon 00:00", "latency_ms": 1200 },
        { "timestamp": "Tue 00:00", "latency_ms": 1350 },
        { "timestamp": "Wed 00:00", "latency_ms": 1100 },
        { "timestamp": "Thu 00:00", "latency_ms": 1450 },
        { "timestamp": "Fri 00:00", "latency_ms": 1600 },
        { "timestamp": "Sat 14:00", "latency_ms": 2100 },
        { "timestamp": "Sun 00:00", "latency_ms": 1300 }
      ]
    }
  }
  ```

---

## 7. Knowledge Graph (Right Pane Graph View)

### GET /workspaces/{workspace_id}/graph
Retrieves the nodes and edges for the OKF knowledge graph visualization.
- **Response 200 (OK):**
  ```json
  {
    "nodes": [
      {
        "id": "entity_1",
        "label": "Candidate A",
        "type": "Person",
        "properties": { "experience": "4 years" }
      },
      {
        "id": "doc_1",
        "label": "Candidate_A_Resume.pdf",
        "type": "Document"
      }
    ],
    "edges": [
      {
        "source": "entity_1",
        "target": "doc_1",
        "label": "MENTIONED_IN",
        "weight": 0.95
      }
    ]
  }
  ```
