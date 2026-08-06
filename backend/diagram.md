# Knowledge Retrieval Engine (KRE) System Architecture

This diagram illustrates the end-to-end workflow of the system, starting from when a user uploads a file, through the data processing pipeline, and finally how a user's question is answered during Q&A. It also highlights the recent infrastructure updates for dynamic AWS local mapping and model-specific region targeting.

```mermaid
flowchart TD
    %% Define Styles
    classDef user fill:#4ade80,stroke:#22c55e,stroke-width:2px,color:#000
    classDef api fill:#60a5fa,stroke:#3b82f6,stroke-width:2px,color:#000
    classDef processing fill:#a78bfa,stroke:#8b5cf6,stroke-width:2px,color:#000
    classDef bedrock fill:#f472b6,stroke:#ec4899,stroke-width:2px,color:#000
    classDef aws fill:#fb923c,stroke:#f97316,stroke-width:2px,color:#000
    classDef storage fill:#fcd34d,stroke:#f59e0b,stroke-width:2px,color:#000

    %% Actors
    UserUpload([User Uploads File]) ::: user
    UserQuery([User Asks Question]) ::: user

    %% ==========================================
    %% INGESTION PIPELINE
    %% ==========================================
    subgraph Ingestion [Ingestion Pipeline]
        UploadAPI[Ingest API Endpoint] ::: api
        Parser[Document Parser & Splitter] ::: processing
        ConceptExtractor[Concept Extractor] ::: processing
        Embedder[Embedding Generator] ::: processing
        
        UploadAPI --> Parser
        Parser --> ConceptExtractor
        ConceptExtractor --> Embedder
    end

    %% ==========================================
    %% Q&A PIPELINE (LangGraph)
    %% ==========================================
    subgraph QueryFlow [Q&A Pipeline]
        QueryAPI[Query API Endpoint] ::: api
        LangGraph[LangGraph Pipeline] ::: processing
        QueryEmbedder[Query Embedder] ::: processing
        Retriever[Vector Retriever] ::: processing
        FastPath{High Confidence?} ::: processing
        Reranker[Document Reranker] ::: processing
        Jaccard[Jaccard Fallback] ::: processing
        LLM[LLM Answer Generator] ::: processing
        Response[Final Answer & Citations] ::: api

        QueryAPI --> LangGraph
        LangGraph --> QueryEmbedder
        QueryEmbedder --> Retriever
        Retriever --> FastPath
        
        FastPath -- Yes --> Response
        FastPath -- No --> Reranker
        
        Reranker -- Exception/Billing Error --> Jaccard
        Jaccard --> LLM
        Reranker -- Success --> LLM
        
        LLM --> Response
    end

    %% ==========================================
    %% AWS BEDROCK MODELS
    %% ==========================================
    subgraph Bedrock [Amazon Bedrock Models]
        BedrockNovaExtract[Nova Micro v1\nap-south-1] ::: bedrock
        BedrockTitanEmbed[Titan Embed v2\nap-south-1] ::: bedrock
        BedrockCohere[Cohere Rerank v3.5\nus-east-1] ::: bedrock
        BedrockNovaGen[Nova Lite v1\nap-south-1] ::: bedrock
    end

    %% ==========================================
    %% LOCALSTACK / AWS INFRASTRUCTURE
    %% ==========================================
    subgraph Infra [Data Persistence - Floci localhost:4566]
        RDS[(AWS RDS\nPostgreSQL)] ::: storage
        ElastiCache[(AWS ElastiCache\nRedis)] ::: storage
        AWSConfig[AWS Client Config\nDynamic Discovery] ::: aws
    end

    %% Wiring Ingestion to Models & Storage
    UserUpload --> UploadAPI
    ConceptExtractor <--> BedrockNovaExtract
    Embedder <--> BedrockTitanEmbed
    Embedder --> AWSConfig
    AWSConfig --> RDS
    AWSConfig --> ElastiCache

    %% Wiring Q&A to Models & Storage
    UserQuery --> QueryAPI
    QueryEmbedder <--> BedrockTitanEmbed
    Retriever <--> AWSConfig
    Reranker <--> BedrockCohere
    LLM <--> BedrockNovaGen
```

## Key Infrastructure Features
1. **Dynamic AWS Local Service Discovery**: The repositories (`postgres.py` and `redis_cache.py`) don't hardcode standard local ports. In `dev` mode, they query the local AWS API (Floci at `localhost:4566`) using boto3 to dynamically extract the active `RDS` and `ElastiCache` endpoints.
2. **Model Region Routing**: 
   - All standard generation and embedding models (`amazon.nova`, `amazon.titan`) are strictly routed to `ap-south-1`.
   - The Reranker (`cohere.rerank`) has a dedicated config file (`reranker_config.py`) that strictly targets `us-east-1` to avoid regional `ValidationExceptions`.
3. **Resilient Pipeline**: If the Reranker fails (e.g., due to AWS Marketplace billing/subscription limits), the system catches the `AccessDeniedException` and gracefully degrades to local deterministic Jaccard word-overlap scoring, ensuring the LLM is never starved of context.
4. **Fast-Path Verification**: The LangGraph pipeline evaluates retrieval confidence. If confidence is overwhelmingly high, it completely skips the LLM and Reranker steps, saving both cost and latency.
