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
    // Mock network latency
    await new Promise(resolve => setTimeout(resolve, 1500));

    // Dummy response for layout testing
    const mockResponse: QueryResponse = {
      answer: "The Indian healthcare sector is expected to grow to USD 280 billion by 2020. This growth is driven by increasing incomes, greater health awareness, lifestyle diseases and increasing access to insurance.",
      retrieval_path: ["BM25", "Vector", "LLM"],
      confidence: 0.89,
      latency_ms: 1540,
      citations: [
        {
          id: "cit_1",
          document_id: "doc_123",
          source_format: ".pdf",
          snippet: "Healthcare in India is expected to reach USD 280 billion...",
          location_reference: "Page 14",
          bounding_box: [100, 150, 400, 200]
        },
        {
          id: "cit_2",
          document_id: "doc_456",
          source_format: ".xlsx",
          snippet: "Row 14: Q3 Healthcare Projections",
          location_reference: "Sheet: Revenue, Row: 14",
          bounding_box: null
        }
      ]
    };

    setResponse(mockResponse);
    setLoading(false);
  };

  return { loading, response, executeQuery };
}
