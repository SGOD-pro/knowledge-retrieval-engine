import re
from typing import List
from kre.models import Chunk

def merge_and_split_chunks(chunks: List[Chunk], min_tokens: int = 100, max_tokens: int = 500) -> List[Chunk]:
    """
    Adjusts chunk sizes to ensure they are between min_tokens and max_tokens.
    - If a paragraph is smaller, merge it with the next one.
    - If larger, split it by sentence.
    Token count is approximated by word count.
    """
    if not chunks:
        return []

    # Step 1: Split large chunks by sentence
    split_chunks = []
    for chunk in chunks:
        word_count = len(chunk.text.split())
        if word_count > max_tokens:
            # Simple sentence splitting (handles basic punctuation)
            sentences = re.split(r'(?<=[.!?])\s+', chunk.text)
            current_text = ""
            sub_index = 0
            for sentence in sentences:
                if len((current_text + " " + sentence).split()) > max_tokens and current_text:
                    new_chunk = Chunk(
                        id=f"{chunk.id}:s{sub_index}",
                        document_id=chunk.document_id,
                        source_format=chunk.source_format,
                        text=current_text.strip(),
                        element_type=chunk.element_type,
                        page_number=chunk.page_number,
                        section_path=chunk.section_path,
                        bounding_box=chunk.bounding_box,
                        location_reference=chunk.location_reference,
                        metadata=chunk.metadata,
                        structural_weight=chunk.structural_weight,
                        provider=chunk.provider,
                    )
                    split_chunks.append(new_chunk)
                    current_text = sentence
                    sub_index += 1
                else:
                    current_text = (current_text + " " + sentence).strip()
            
            if current_text:
                new_chunk = Chunk(
                    id=f"{chunk.id}:s{sub_index}",
                    document_id=chunk.document_id,
                    source_format=chunk.source_format,
                    text=current_text.strip(),
                    element_type=chunk.element_type,
                    page_number=chunk.page_number,
                    section_path=chunk.section_path,
                    bounding_box=chunk.bounding_box,
                    location_reference=chunk.location_reference,
                    metadata=chunk.metadata,
                    structural_weight=chunk.structural_weight,
                    provider=chunk.provider,
                )
                split_chunks.append(new_chunk)
        else:
            split_chunks.append(chunk)

    # Step 2: Merge small chunks
    merged_chunks = []
    current_chunk = None

    for chunk in split_chunks:
        if current_chunk is None:
            current_chunk = chunk
            continue
        
        current_words = len(current_chunk.text.split())
        
        if current_words < min_tokens:
            # Merge text
            merged_text = current_chunk.text + " " + chunk.text
            
            # Create a new chunk that inherits properties from the first one
            current_chunk = Chunk(
                id=current_chunk.id,  # keep the original id of the first piece
                document_id=current_chunk.document_id,
                source_format=current_chunk.source_format,
                text=merged_text,
                element_type="paragraph", # it's mixed now, default to paragraph
                page_number=current_chunk.page_number,
                section_path=current_chunk.section_path,
                bounding_box=current_chunk.bounding_box,
                location_reference=current_chunk.location_reference,
                metadata=current_chunk.metadata,
                structural_weight=current_chunk.structural_weight,
                provider=current_chunk.provider,
            )
        else:
            merged_chunks.append(current_chunk)
            current_chunk = chunk

    if current_chunk:
        merged_chunks.append(current_chunk)

    return merged_chunks
