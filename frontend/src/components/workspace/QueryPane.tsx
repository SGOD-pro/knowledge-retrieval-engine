import React, { useState } from 'react';
import type { QueryResponse } from '@/hooks/useQueryEngine';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { Badge } from '@/components/ui/badge';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Send, Zap, BrainCircuit, Loader2 } from 'lucide-react';

interface QueryPaneProps {
  onSearch: (query: string) => void;
  loading: boolean;
  response: QueryResponse | null;
}

export function QueryPane({ onSearch, loading, response }: QueryPaneProps) {
  const [input, setInput] = useState('');

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (input.trim() && !loading) {
      onSearch(input.trim());
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit(e);
    }
  };

  const isFastPath = response?.retrieval_path && !response.retrieval_path.includes("LLM");

  return (
    <div className="h-full flex flex-col bg-background relative">
      <div className="flex-1 flex flex-col items-center justify-center p-8 max-w-3xl mx-auto w-full">
        {!response && !loading && (
          <div className="text-center mb-12 opacity-80">
            <h2 className="font-serif text-4xl mb-4 tracking-tight">KRE Intelligence</h2>
            <p className="text-muted-foreground">Ask any question about your organization's documents.</p>
          </div>
        )}

        <ScrollArea className="w-full flex-1 mb-6">
          {loading && (
            <div className="flex items-center justify-center h-40 text-primary">
              <Loader2 className="w-8 h-8 animate-spin" />
            </div>
          )}

          {response && !loading && (
            <div className="w-full animate-in fade-in slide-in-from-bottom-4 duration-500">
              <div className="flex items-center gap-3 mb-6">
                <Badge variant="outline" className={`font-mono ${isFastPath ? 'text-blue-500 border-blue-500/30' : 'text-primary border-primary/30'}`}>
                  {isFastPath ? <Zap className="w-3 h-3 mr-1" /> : <BrainCircuit className="w-3 h-3 mr-1" />}
                  {isFastPath ? 'Fast Match' : 'Reasoned Answer'}
                </Badge>
                
                <div className="flex items-center gap-1.5">
                  {response.retrieval_path.map((step, i) => (
                    <React.Fragment key={step}>
                      <Badge variant="secondary" className="text-[10px] uppercase tracking-wider font-semibold rounded-full px-2">
                        {step}
                      </Badge>
                      {i < response.retrieval_path.length - 1 && (
                        <span className="text-muted-foreground/40 text-xs">→</span>
                      )}
                    </React.Fragment>
                  ))}
                </div>
                
                <div className="ml-auto text-xs text-muted-foreground font-mono">
                  {response.latency_ms}ms
                </div>
              </div>

              <div className="prose prose-slate dark:prose-invert max-w-none text-lg leading-relaxed text-foreground/90">
                {response.answer}
              </div>
            </div>
          )}
        </ScrollArea>
        
        <div className="w-full relative mt-auto">
          <form onSubmit={handleSubmit} className="relative group">
            <Textarea 
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Ask a question about the document..."
              className="resize-none pr-14 py-4 min-h-[60px] rounded-2xl bg-card border-border/50 shadow-sm transition-all focus-visible:ring-primary focus-visible:border-primary"
              rows={1}
            />
            <Button 
              size="icon" 
              type="submit" 
              disabled={!input.trim() || loading}
              className="absolute right-2 bottom-2 rounded-xl transition-transform active:scale-95"
            >
              <Send className="w-4 h-4" />
            </Button>
          </form>
          <div className="text-center mt-3 text-xs text-muted-foreground">
            AI can make mistakes. Verify important information.
          </div>
        </div>
      </div>
    </div>
  );
}
