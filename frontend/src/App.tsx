import React, { useState } from 'react';
import { useAuth } from '@/contexts/AuthContext';
import { useTheme } from '@/contexts/ThemeProvider';
import { useQueryEngine, type Citation } from '@/hooks/useQueryEngine';
import { DocumentViewer } from '@/components/workspace/DocumentViewer';
import { QueryPane } from '@/components/workspace/QueryPane';
import { CitationList } from '@/components/workspace/CitationList';
import { Button } from '@/components/ui/button';
import { Moon, Sun, LogOut, Database } from 'lucide-react';

function App() {
  const { isAuthenticated, login, logout } = useAuth();
  const { theme, setTheme } = useTheme();
  const { loading, response, executeQuery } = useQueryEngine();
  const [selectedCitation, setSelectedCitation] = useState<Citation | null>(null);

  // Update selected citation when response changes
  React.useEffect(() => {
    if (response?.citations?.length) {
      setSelectedCitation(response.citations[0]);
    }
  }, [response]);

  if (!isAuthenticated) {
    return (
      <div className="min-h-screen bg-background flex flex-col items-center justify-center p-4">
        <div className="bg-card p-8 rounded-2xl shadow-sm border max-w-sm w-full text-center">
          <Database className="w-12 h-12 text-primary mx-auto mb-6" />
          <h1 className="font-serif text-2xl mb-2">KRE Login</h1>
          <p className="text-muted-foreground text-sm mb-8">
            Dummy auth interface. Ready for OAuth2.1 integration.
          </p>
          <Button onClick={login} className="w-full h-12 text-md">
            Sign In to Workspace
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="h-screen w-full flex flex-col overflow-hidden bg-background font-sans">
      {/* Minimal Header */}
      <header className="h-14 border-b flex items-center justify-between px-6 bg-card shrink-0">
        <div className="flex items-center gap-2">
          <Database className="w-5 h-5 text-primary" />
          <span className="font-serif font-medium text-lg tracking-tight text-primary">KRE Intelligence</span>
        </div>
        <div className="flex items-center gap-2">
          <Button 
            variant="ghost" 
            size="icon" 
            onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
            className="text-muted-foreground hover:text-foreground"
          >
            {theme === 'dark' ? <Sun className="w-4 h-4" /> : <Moon className="w-4 h-4" />}
          </Button>
          <Button 
            variant="ghost" 
            size="icon" 
            onClick={logout}
            className="text-muted-foreground hover:text-foreground"
          >
            <LogOut className="w-4 h-4" />
          </Button>
        </div>
      </header>

      {/* 3-Pane Workspace */}
      <main className="flex-1 grid grid-cols-[1fr_2fr_300px] overflow-hidden">
        <DocumentViewer selectedCitation={selectedCitation} />
        
        <QueryPane 
          onSearch={executeQuery} 
          loading={loading} 
          response={response} 
        />
        
        <CitationList 
          citations={response?.citations || []}
          onSelectCitation={setSelectedCitation}
          selectedCitationId={selectedCitation?.id}
        />
      </main>
    </div>
  );
}

export default App;
