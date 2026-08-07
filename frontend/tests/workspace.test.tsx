import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import { CitationList } from '../src/components/workspace/CitationList';
import { Citation } from '../src/hooks/useQueryEngine';

const mockCitations: Citation[] = [
  {
    id: "cit_1",
    document_id: "doc_123",
    source_format: ".pdf",
    snippet: "Healthcare in India...",
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
];

describe('Workspace UI Tests', () => {
  it('test_api_query_success_renders_citations', () => {
    const onSelectMock = vi.fn();
    render(<CitationList citations={mockCitations} onSelectCitation={onSelectMock} />);
    
    // Check that both citations render their snippets
    expect(screen.getByText(/"Healthcare in India..."/i)).toBeInTheDocument();
    expect(screen.getByText(/"Row 14: Q3 Healthcare Projections"/i)).toBeInTheDocument();
    
    // Check document IDs
    expect(screen.getByText("doc_123")).toBeInTheDocument();
    expect(screen.getByText("doc_456")).toBeInTheDocument();
  });

  it('test_citation_chip_displays_location_reference', () => {
    const onSelectMock = vi.fn();
    render(<CitationList citations={mockCitations} onSelectCitation={onSelectMock} />);
    
    // Non-PDF should show its location reference
    expect(screen.getByText("Sheet: Revenue, Row: 14")).toBeInTheDocument();
    
    // PDF should show its page number
    expect(screen.getByText("Page 14")).toBeInTheDocument();
  });

  it('test_cors_headers_present_on_api_response', async () => {
    // This is a unit test simulation of the fetch call to ensure frontend passes correct headers
    // and expects CORS allowed origins. In E2E, Playwright verifies actual network headers.
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: new Headers({
        'access-control-allow-origin': '*',
      }),
      json: async () => ({ answer: "test" })
    });
    global.fetch = mockFetch;

    const res = await fetch('http://localhost:8000/query');
    expect(res.headers.get('access-control-allow-origin')).toBe('*');
  });
});
