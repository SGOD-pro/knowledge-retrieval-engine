"""Provider abstraction layer for KRE.

All external model calls (embedding, reranking, LLM) must route through this package.
Direct import of OpenRouter or Bedrock SDKs outside this package is strictly prohibited (Rule 28).
"""
