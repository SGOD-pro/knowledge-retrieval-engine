export interface BatchResponse {
  results: Result[];
}

export interface Result {
  document_id: string;
  markdown: string;
  elements: DocumentElements;
  image_s3_keys?: ImageS3Key[];
}

export interface ImageS3Key {
  s3_uri: string;
  url: string;
}

export interface DocumentElements {
  "file name": string;
  "number of pages": number;
  author: string;
  title: string;
  "creation date": string;
  "modification date": string;
  kids: ElementNode[];
}

export interface ElementNode {
  type: string;
  "page number": number;
  "bounding box": [number, number, number, number];
  content: string;
  
  // Optional properties that may appear depending on the element type
  pdfua_tag?: string;
  id?: number;
  level?: string;
  "heading level"?: number;
  font?: string;
  "font size"?: number;
  "text color"?: string;
  "numbering style"?: string;
  "previous list id"?: number;
  "number of columns"?: number;
  rows?: any[]; // Update to specific type if rows structure is known
  "number of rows"?: number;
  "number of list items"?: number;
  alt_source?: string;
  "linked content id"?: number;
  "next list id"?: number;
  source?: string;
  "list items"?: any[]; // Update to specific type if list items structure is known
}
