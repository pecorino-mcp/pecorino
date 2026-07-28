import logging
from typing import Optional
from mcp import types
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

    # Try IDE sampling first
    if ctx and ctx.supports_sampling:
        try:
            logger.info("Attempting to generate Cypher using IDE sampling...")
            result = await ctx.create_message(
                messages=[
                    types.SamplingMessage(
                        role="user", 
                        content=types.TextContent(type="text", text=prompt)
                    )
                ],
                system_prompt=system_prompt,
                max_tokens=256,
            )
            
            if result and result.content:
                # Extract text content from the response
                text_content = ""
                for msg_part in result.content:
                    if hasattr(msg_part, "text"):
                        text_content += msg_part.text
                    elif isinstance(msg_part, dict) and "text" in msg_part:
                        text_content += msg_part["text"]
                    elif isinstance(msg_part, str):
                        text_content += msg_part
                
                if text_content:
                    cypher = text_content.strip()
                    # Clean up markdown code blocks if the LLM ignored Rule 1
                    if cypher.startswith("```cypher"):
                        cypher = cypher[9:]
                    elif cypher.startswith("```"):
                        cypher = cypher[3:]
                    if cypher.endswith("```"):
                        cypher = cypher[:-3]
                    return cypher.strip()
        except Exception as e:
            logger.warning(f"IDE sampling failed: {e}. Falling back to local model.")
            
    # Fallback to litellm (local model)
    try:
        import litellm
        import os
        logger.info("Using litellm fallback for Cypher generation...")
        
        # Default to a generic local ollama model or get from environment
        fallback_model = os.getenv("PECORINO_LLM_MODEL", "ollama/llama3")
        
        response = litellm.completion(
            model=fallback_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            max_tokens=256
        )
        
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
