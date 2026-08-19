import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { Workspace } from '../src/pages/Workspace'

vi.mock("react-resizable-panels", () => ({
  Group: ({ children }: any) => <div>{children}</div>,
  PanelGroup: ({ children }: any) => <div>{children}</div>,
  Panel: ({ children }: any) => <div>{children}</div>,
  PanelResizeHandle: () => <div />,
  Separator: () => <div />
}))

const mockCitationResponse = {
  answer: "This is a mocked answer from the agent.",
  citations: [
    {
      chunk_id: "cit_1",
      document_id: "doc_123",
      source_format: "pdf",
      bounding_box: { page_number: 14 },
      location_reference: "",
      text_snippet: "Healthcare in India..."
    },
    {
      chunk_id: "cit_2",
      document_id: "doc_456",
      source_format: "xlsx",
      bounding_box: null,
      location_reference: "Sheet: Revenue, Row: 14",
      text_snippet: "Row 14: Q3 Healthcare Projections"
    }
  ],
  confidence_score: 0.9,
  latency_breakdown: { total_ms: 1200 },
  fast_path: false,
  cached: false,
  document_ids: [],
  retrieval_path: ["bm25", "vector", "reranker"]
}

describe('Workspace UI Tests', () => {
  it('test_workspace_renders_chat_and_sources_panes', () => {
    render(<Workspace />)
    
    // Check initial state
    expect(screen.getByPlaceholderText(/Ask the knowledge base/i)).toBeInTheDocument()
    expect(screen.getByText("Sources")).toBeInTheDocument()
    expect(screen.getByText("Click a citation to view source document")).toBeInTheDocument()
  })

  it('test_api_query_success_renders_citations_and_badges', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      text: async () => JSON.stringify(mockCitationResponse),
      json: async () => mockCitationResponse
    })
    global.fetch = mockFetch

    render(<Workspace />)
    
    // Submit query
    const input = screen.getByPlaceholderText(/Ask the knowledge base/i)
    fireEvent.change(input, { target: { value: "Test query" } })
    
    const sendButton = input.closest('form')?.querySelector('button[type="submit"]')
    expect(sendButton).not.toBeDisabled()
    
    if (sendButton) {
      fireEvent.click(sendButton)
    }

    // Wait for response to render
    await waitFor(() => {
      expect(screen.getByText("This is a mocked answer from the agent.")).toBeInTheDocument()
    })

    // Check retrieval path badges
    expect(screen.getByText("bm25")).toBeInTheDocument()
    expect(screen.getByText("vector")).toBeInTheDocument()
    expect(screen.getByText("reranker")).toBeInTheDocument()
    
    // Check citations in right pane
    expect(screen.getByText(/Healthcare in India\.\.\./i)).toBeInTheDocument()
    expect(screen.getByText(/Row 14: Q3 Healthcare Projections/i)).toBeInTheDocument()
    
    // Check citation formatting (PDF vs XLSX)
    expect(screen.getByText("Page 14")).toBeInTheDocument()
    expect(screen.getByText("Sheet: Revenue, Row: 14")).toBeInTheDocument()
  })
})
