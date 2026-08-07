import React from 'react';
import type { Citation } from '@/hooks/useQueryEngine';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent } from '@/components/ui/card';
import { ScrollArea } from '@/components/ui/scroll-area';

interface CitationListProps {
  citations: Citation[];
  onSelectCitation: (citation: Citation) => void;
  selectedCitationId?: string;
}

export function CitationList({ citations, onSelectCitation, selectedCitationId }: CitationListProps) {
  if (!citations || citations.length === 0) {
    return (
      <div className="h-full flex flex-col bg-[#181715] text-[#efe9de] border-l border-border">
        <div className="p-4 border-b border-white/10 font-medium text-sm text-[#efe9de]/70">
          Sources
        </div>
        <div className="flex-1 flex items-center justify-center text-sm text-white/30">
          No citations available.
        </div>
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col bg-[#181715] text-[#efe9de] border-l border-border">
      <div className="p-4 border-b border-white/10 font-medium text-sm flex justify-between items-center">
        <span className="text-[#efe9de]/90">Cited Sources</span>
        <Badge variant="secondary" className="bg-white/10 hover:bg-white/20 text-[#efe9de] font-mono text-xs">
          {citations.length}
        </Badge>
      </div>

      <ScrollArea className="flex-1 p-4">
        <div className="space-y-4">
          {citations.map((citation) => (
            <Card 
              key={citation.id} 
              onClick={() => onSelectCitation(citation)}
              className={`cursor-pointer transition-colors border-0 bg-[#252320] hover:bg-[#302d29] ${
                selectedCitationId === citation.id ? 'ring-2 ring-primary ring-offset-2 ring-offset-[#181715]' : ''
              }`}
            >
              <CardContent className="p-4">
                <div className="flex items-center gap-2 mb-3">
                  <Badge className="bg-primary/20 text-primary hover:bg-primary/30 border-0 rounded-sm px-1.5 py-0">
                    {citation.source_format.replace('.', '').toUpperCase()}
                  </Badge>
                  <span className="text-xs text-white/50 font-mono truncate">
                    {citation.document_id}
                  </span>
                </div>
                
                <p className="text-sm text-[#efe9de]/90 line-clamp-3 mb-3 leading-relaxed">
                  "{citation.snippet}"
                </p>

                <div className="flex items-center text-xs text-white/40 font-mono">
                  <span className="mr-2 opacity-50">¶</span>
                  {citation.location_reference}
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      </ScrollArea>
    </div>
  );
}
