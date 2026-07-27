# PROJECT.md — Knowledge Retrieval Engine (KRE)

## What We Are Building

An enterprise document intelligence platform where every answer is traceable to an exact page and paragraph in the source PDF.

Not a chatbot. A staged retrieval engine with a typed knowledge layer that makes document search feel like talking to someone who has read every page — and can show you exactly which page they're quoting.

### Core Innovation
Three things working together that no existing system combines correctly:

1. **PageIndex** — a structural positional index that treats document hierarchy (`Document → Page → Section → Paragraph`) as a first-class retrieval signal, not an afterthought.
2. **OKF (Ontology-driven Knowledge Framework)** — a typed semantic layer above the graph that stores not just edges but concept types, properties, and semantic roles. The graph is one view over OKF, not the knowledge itself.
3. **Staged Hybrid Retrieval** — cheapest filter runs first. BM25 before vector search. PageIndex before graph. Graph only when the query actually needs relationship traversal. No unnecessary computation.

### Tech Stack
```text
Backend:   FastAPI + LangGraph + Python
Parsing:   opendataloader-pdf (Apache-2.0, local, CPU-only)
Retrieval: BM25 (rank-bm25) → PageIndex → pgvector (Titan V2) → OKF Lookup → Graph (conditional) → Cohere Rerank 3.5 / NVIDIA API
LLM:       Single call, structured output, max 1200 tokens context
Storage:   PostgreSQL + FAISS (local) + Redis
Frontend:  Next.js 3-pane workspace
```

---

## Business Impact

Document search fails enterprises because:
- Vector RAG returns plausible but unverifiable answers.
- BM25 misses semantic matches.
- Neither shows **WHY** the answer was retrieved.

Regulated industries (legal, healthcare, finance, government) reject AI assistants specifically because answers cannot be verified. A paralegal or compliance analyst cannot submit an AI answer they cannot audit. KRE's citation-to-bounding-box pipeline makes every answer verifiable in under 3 seconds. That single capability unlocks markets existing RAG tools cannot enter.

### Current Alternative Costs:
- **Manual PDF remediation:** $50–200 per document.
- **Enterprise document search tools** (Elasticsearch, Azure AI Search): $50k–200k/year, no semantic understanding.
- **Consultants reviewing contracts:** $300–500/hour.

---

## Competitive Landscape

| Competitor | Approach | Core Failure |
| :--- | :--- | :--- |
| **ChatGPT + PDF** | Flat vector RAG | Unverifiable, no source pinning |
| **Perplexity** | Web search + LLM | Not document-private |
| **Notion AI** | Flat keyword in notes | No hierarchy, no semantic layer |
| **LlamaIndex** | Framework only | User assembles, no quality guarantee |
| **Azure AI Search** | BM25 + vector hybrid | Black box, no audit trail |
| **Vertex AI Search** | Google-managed RAG | Cloud-locked, no structural index |
| **Elasticsearch** | BM25 + dense vector | No document hierarchy, no OKF |

---

## What Makes KRE Hard to Copy

Copying individual components is trivial. Copying the system is not, for three reasons:

1. **PageIndex structural scoring is not a library you install.**  
   It requires a defined schema (`element_type` weights, section depth decay, heading anchor scoring) built specifically for your document hierarchy. Competitors using off-the-shelf BM25 don't have it.

2. **OKF Knowledge Layer requires domain-specific ontology design.**  
   The concept types, property schemas, and relation taxonomies are decisions that accumulate over real document corpora. A competitor starting fresh has no typed knowledge base.

3. **Retrieval audit trail creates institutional trust lock-in.**  
   Once a compliance team builds workflows around verifiable answers with bounding-box citations, switching to an opaque system is a policy decision, not just a technical one.

The long-term moat is feedback data: which retrieval paths produce correct answers for which query types, accumulated over thousands of real queries. This is the training data for a better planner — one you can build, and a competitor starting today cannot.

---

## What This Is Not (v1)
- Not a chatbot or conversational assistant.
- Not a document editor or annotation tool.
- Not a multi-modal image analysis system.
- Not a multi-tenant SaaS with authentication (v2).
- Not a real-time document sync system.
- Not a SQL / structured query interface.

