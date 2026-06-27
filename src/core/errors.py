class PecorinoError(Exception):
    """Base exception for all Pecorino errors."""
    pass

class SecurityValidationError(PecorinoError):
    """Raised when a path validation or security guard fails."""
    pass

class TargetNotFoundError(PecorinoError, FileNotFoundError):
    """Raised when a target file or directory is not found."""
    pass

class IndexNotFoundError(PecorinoError):
    """Raised when the database index or full-text search index is missing."""
    pass

class AnalysisError(PecorinoError):
    """Raised when AST parsing, metrics extraction, or graph database queries fail."""
    pass
