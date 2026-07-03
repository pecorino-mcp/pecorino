import os
from pathlib import Path
from src.mcp_server.config import settings
from src.core.errors import TargetNotFoundError, SecurityValidationError

workspace_root = Path(__file__).resolve().parent.parent.parent.parent
ALLOWED_OUTPUT = workspace_root / ".mcp_outputs"
ALLOWED_OUTPUT.mkdir(exist_ok=True)
MAX_READ_BYTES = 1_000_000  # 1 MB

SUSPICIOUS_PATTERNS = ("ignore previous", "system prompt", "you are now",
                       "disregard", "forget your instructions")
STRICT_INJECTION_CHECK = os.getenv("PECORINO_STRICT_INJECTION_CHECK", "").lower() in ("true", "1", "yes")

def is_project_workspace(path: Path) -> bool:
    """Check if the path resides inside a project workspace.
    
    A project workspace is defined as any directory that contains common project 
    marker files/folders (like .git, .vscode, package.json, pyproject.toml) in its hierarchy.
    """
    try:
        current = path if path.is_dir() else path.parent
        visited = set()
        
        while current and current != current.parent:
            current_resolved = current.resolve()
            if current_resolved in visited:
                break
            visited.add(current_resolved)
            
            if (current / ".git").is_dir() or (current / ".vscode").is_dir() or (current / ".idea").is_dir():
                return True
                
            for marker in ("pyproject.toml", "package.json", "Cargo.toml", "go.mod", "Makefile", "requirements.txt", "setup.py"):
                if (current / marker).is_file():
                    return True
                    
            current = current.parent
            
        return False
    except Exception:
        return False


def is_safe_path(p: str, allow_external: bool = False) -> bool:
    """Validate path safety with optional external access.
    
    Allows:
    1. Paths within settings.workspace_root.
    2. Paths within the current working directory (Path.cwd()).
    3. Paths inside recognized project workspaces (checked via is_project_workspace).
    4. Allowlisted external paths if allow_external=True.
    """
    try:
        target = Path(p).expanduser().resolve()
        
        # 1. Check if within workspace (always allowed)
        if target.is_relative_to(settings.workspace_root):
            return True
            
        # 2. Check if within current working directory (always allowed)
        try:
            if target.is_relative_to(Path.cwd().resolve()):
                return True
        except (ValueError, RuntimeError):
            pass

        # 3. Check if inside a project workspace
        if is_project_workspace(target):
            return True

        # 4. External access checks when allow_external=True
        if allow_external:
            # Allowlist model: only roots set via PECORINO_ALLOWED_EXTERNAL_DIRS
            if not settings.allowed_external_roots:
                return False
            for allowed_root in settings.allowed_external_roots:
                try:
                    if target.is_relative_to(allowed_root):
                        return True
                except ValueError:
                    continue
            return False
        
        return False
    except Exception:
        return False


def safe_path(p: str, allow_external: bool = False) -> Path:
    """Resolve and validate a path, ensuring it's safe."""
    if not p:
        p = "."
    path = Path(p).expanduser().resolve()

    if not path.exists():
        raise TargetNotFoundError(f"Not found: {path}")

    # Must be safe (no directory traversal out of workspace roots).
    if not is_safe_path(str(path), allow_external):
        raise SecurityValidationError(f"Path outside allowed workspace: {path}")

    return path


def safe_output_path(p: str) -> Path:
    """Validate output path — must be a relative filename (placed in ALLOWED_OUTPUT)
    or an absolute path already under ALLOWED_OUTPUT."""
    if Path(p).is_absolute():
        out = Path(p).resolve()
    else:
        out = (ALLOWED_OUTPUT / Path(p).name).resolve()
    if not out.is_relative_to(ALLOWED_OUTPUT):
        raise SecurityValidationError(
            f"output_file must be a relative filename (written to {ALLOWED_OUTPUT}) "
            f"or an absolute path under {ALLOWED_OUTPUT}. Got: {p}"
        )
    return out


def read_limited(p: Path) -> str:
    """Read file content with a hard size cap to prevent DoS."""
    with p.open('rb') as f:
        data = f.read(MAX_READ_BYTES + 1)
    if len(data) > MAX_READ_BYTES:
        raise SecurityValidationError(f"File too large (>{MAX_READ_BYTES} bytes): {p.name}")
    return data.decode('utf-8', errors='ignore')


def check_suspicious(value: str, param_name: str) -> None:
    """Reject values containing patterns that look like prompt injection.
    Gated behind PECORINO_STRICT_INJECTION_CHECK env var (disabled by default).
    The output wrapping instruction is the primary mitigation."""
    if not STRICT_INJECTION_CHECK:
        return
    if isinstance(value, str) and any(s in value.lower() for s in SUSPICIOUS_PATTERNS):
        raise SecurityValidationError(f"Potential prompt injection detected in {param_name}")
