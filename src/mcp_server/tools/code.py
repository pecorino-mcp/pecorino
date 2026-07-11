import asyncio
import logging
from typing import Optional

from src.core.errors import SecurityValidationError
from src.mcp_server.middleware.security import safe_path, check_suspicious
from mcp.server import ServerRequestContext

logger = logging.getLogger(__name__)

MAX_LINES_LIMIT = 2000

async def do_get_code_range(
    target: str,
    start_line: int,
    end_line: int,
    start_byte: int = 0,
    end_byte: int = 0,
    allow_external: bool = False,
    ctx: Optional[ServerRequestContext] = None
) -> dict:
    """Retrieve a precise range of lines or bytes from a specific file."""
    if start_line < 1:
        raise SecurityValidationError("start_line must be >= 1")
    if end_line < start_line:
        raise SecurityValidationError("end_line must be >= start_line")
        
    if (end_line - start_line) > MAX_LINES_LIMIT:
        raise SecurityValidationError(f"Requested line range exceeds the maximum limit of {MAX_LINES_LIMIT} lines.")

    check_suspicious(target, "target")
    path = safe_path(target, allow_external)
    
    if not path.is_file():
        raise SecurityValidationError(f"Target is not a file or does not exist: {path}")
    
    def _read_content():
        try:
            if start_byte > 0 and end_byte > start_byte:
                with open(path, 'rb') as f:
                    content_bytes = f.read()
                    return content_bytes[start_byte:end_byte].decode('utf-8', errors='replace')
            else:
                with open(path, 'r', encoding='utf-8', errors='replace') as f:
                    lines = f.readlines()
                    # 1-indexed to 0-indexed translation
                    start_idx = start_line - 1
                    end_idx = min(end_line, len(lines))
                    
                    selected = lines[start_idx:end_idx]
                    if not selected:
                        return ""
                    return "".join(selected)
        except Exception as e:
            raise SecurityValidationError(f"Failed to read file {path}: {str(e)}")

    content = await asyncio.to_thread(_read_content)
    
    return {
        "target": str(path),
        "start_line": start_line,
        "end_line": end_line,
        "start_byte": start_byte,
        "end_byte": end_byte,
        "content": content,
        "status": "ok"
    }
