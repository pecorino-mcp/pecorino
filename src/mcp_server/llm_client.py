import logging
from typing import Optional
import mcp_types as types
from src.mcp_server.context_helper import PecorinoContext

logger = logging.getLogger(__name__)

async def generate_cypher(
    natural_language_query: str, 
    schema: str, 
    ctx: PecorinoContext
) -> Optional[str]:
    """
    Generate a Cypher query from natural language, using IDE sampling first,
    and falling back to a local model (via litellm) if sampling is unavailable or fails.
    """
    system_prompt = f"""You are a KùzuDB Cypher expert. Convert the user's question into a valid Cypher query.
Schema:
{schema}

Rules:
1. Return ONLY the raw Cypher query, no markdown blocks, no explanation.
2. Use CONTAINS for case-sensitive substring matching (do not use regexes).
3. Always LIMIT results to 50 unless specified.
4. Kuzu does not support the Neo4j CALL db.labels() or db.schema().
5. For CodeNode, you can match on `f.name` or `f.kind`.
"""
    
    prompt = f"Question: {natural_language_query}\n"

    # Use litellm (local model)
    try:
        import litellm
        import os
        import sys
        logger.info("Using litellm for Cypher generation...")
        
        fallback_model = os.getenv("PECORINO_LLM_MODEL", "ollama/llama3")
        
        # Suppress litellm stdout print statements which corrupt MCP stream
        litellm.suppress_debug_info = True
        old_stdout = sys.stdout
        sys.stdout = sys.stderr
        try:
            response = litellm.completion(
                model=fallback_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=256
            )
        finally:
            sys.stdout = old_stdout
            
        cypher = response.choices[0].message.content.strip()
        # Clean up markdown
        if cypher.startswith("```cypher"):
            cypher = cypher[9:]
        elif cypher.startswith("```"):
            cypher = cypher[3:]
        if cypher.endswith("```"):
            cypher = cypher[:-3]
        return cypher.strip()
    except ImportError:
        logger.error("litellm is not installed, cannot use fallback model.")
    except Exception as e:
        logger.error(f"litellm fallback failed: {e}")
        
    return None
