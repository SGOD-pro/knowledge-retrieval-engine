import { useState } from 'react';

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

  const executeQuery = async (query: string) => {
    setLoading(true);
    try {
      const res = await fetch('/api/query', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query })
      });
      
      if (!res.ok) {
        throw new Error('Query failed with status: ' + res.status);
      }
      
      const data = await res.json();
      
      const mappedResponse: QueryResponse = {
        answer: data.answer || "No answer returned.",
        citations: data.citations || [],
        retrieval_path: data.fast_path ? ["BM25", "Vector"] : ["BM25", "Vector", "LLM"],
        confidence: data.confidence_score || 0,
        latency_ms: data.latency_breakdown?.total_ms || 0
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
