import React, { useState } from 'react';
import type { Citation } from '@/hooks/useQueryEngine';

export function DocumentViewer({ selectedCitation }: { selectedCitation: Citation | null }) {
  // We'll mock the PDF rendering with a placeholder block for now,
  // showing how the bounding box would overlay it.
  
  return (
    <div className="h-full flex flex-col bg-background border-r">
      <div className="p-4 border-b font-medium text-sm flex justify-between items-center bg-card">
        <span>Document Viewer</span>
        {selectedCitation?.source_format && (
          <span className="text-xs text-muted-foreground uppercase">{selectedCitation.source_format}</span>
        )}
      </div>
      
      <div className="flex-1 overflow-auto p-4 flex items-center justify-center relative bg-muted/30">
        {!selectedCitation ? (
          <div className="text-muted-foreground text-sm">Select a citation to view document</div>
        ) : selectedCitation.source_format === '.pdf' ? (
          <div className="relative bg-white w-[600px] h-[800px] shadow-sm border text-black/50 p-8">
            <div className="text-center mb-8 border-b pb-4">Mock PDF Page Render</div>
            
            <p className="mb-4">
              Lorem ipsum dolor sit amet, consectetur adipiscing elit. Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua.
            </p>
            <p className="mb-4">
              Healthcare in India is expected to reach USD 280 billion. This growth is driven by increasing incomes, greater health awareness, lifestyle diseases and increasing access to insurance.
            </p>

            {/* Bounding Box Overlay mock */}
            {selectedCitation.bounding_box && (
              <div 
                className="absolute bg-primary/30 border-primary border"
                style={{
                  left: '30px', 
                  top: '120px', 
                  width: '540px', 
                  height: '60px'
                  // Real implementation would use:
                  // left: selectedCitation.bounding_box[0],
                  // top: selectedCitation.bounding_box[1],
                  // width: selectedCitation.bounding_box[2] - selectedCitation.bounding_box[0],
                  // height: selectedCitation.bounding_box[3] - selectedCitation.bounding_box[1],
                }}
              />
            )}
          </div>
        ) : (
          <div className="bg-card p-8 border rounded-xl shadow-sm text-center max-w-md">
            <h3 className="font-semibold text-lg mb-2">Non-PDF Source</h3>
            <p className="text-sm text-muted-foreground mb-4">
              Visual rendering is not available for {selectedCitation.source_format} files.
            </p>
            <div className="bg-muted p-4 rounded-md text-left text-sm font-mono text-primary">
              {selectedCitation.location_reference}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
