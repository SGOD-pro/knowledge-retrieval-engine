import { useState } from 'react';
import { api } from '../lib/api';

export interface Citation {
  id: string;
  document_id: string;
  source_format: string;
  snippet: string;
  location_reference: string;
  bounding_box?: [number, number, number, number] | null;
}

export interface QueryResponse {
  answer: string;
  citations: Citation[];
  retrieval_path: string[];
  confidence: number;
  latency_ms: number;
}

export function useQueryEngine() {
  const [loading, setLoading] = useState(false);
  const [response, setResponse] = useState<QueryResponse | null>(null);

  const executeQuery = async (queryText: string) => {
    setLoading(true);
    try {
      const data = await api.query({ query: queryText });

      const mappedCitations: Citation[] = (data.citations || []).map((c: any, idx: number) => ({
        id: String(c.id || idx + 1),
        document_id: String(c.document_id || "doc_1"),
        source_format: c.source_format || "pdf",
        snippet: c.text || c.snippet || "",
        location_reference: c.location_reference || (c.page_number ? `Page ${c.page_number}` : ""),
        bounding_box: Array.isArray(c.bounding_box)
          ? c.bounding_box
          : c.bounding_box
          ? [c.bounding_box.x, c.bounding_box.y, c.bounding_box.width, c.bounding_box.height]
          : null
      }));

      const mappedResponse: QueryResponse = {
        answer: data.answer || "No answer returned.",
        citations: mappedCitations,
        retrieval_path: data.fast_path ? ["BM25", "Vector"] : ["BM25", "Vector", "LLM"],
        confidence: data.confidence || data.confidence_score || 0,
        latency_ms: data.latency_ms || data.latency_breakdown?.total_ms || 0
      };

      setResponse(mappedResponse);
    } catch (err) {
      console.error("Failed to execute query:", err);
      // Ensure we don't leave the UI in a broken state if the request fails
      setResponse({
        answer: "Failed to connect to the backend API.",
        citations: [],
        retrieval_path: ["Error"],
        confidence: 0,
        latency_ms: 0
      });
    } finally {
      setLoading(false);
    }
  };

  return { loading, response, executeQuery };
}
