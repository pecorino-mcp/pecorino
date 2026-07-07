class PecorinoError(Exception):
    """Base exception for all Pecorino errors."""
    pass

class SecurityValidationError(PecorinoError):
    """Raised when a path validation or security guard fails.

    Optional ``valid_values`` and ``suggestion`` let the error handler
    build structured JSON responses that help LLMs self-correct.
    """

    def __init__(
        self,
        message: str,
        *,
        valid_values: list[str] | None = None,
        suggestion: str | None = None,
    ):
        super().__init__(message)
        self.valid_values = valid_values
        self.suggestion = suggestion

class TargetNotFoundError(PecorinoError, FileNotFoundError):
    """Raised when a target file or directory is not found."""
    pass

class IndexNotFoundError(PecorinoError):
    """Raised when the database index or full-text search index is missing."""
    pass

class AnalysisError(PecorinoError):
    """Raised when AST parsing, metrics extraction, or graph database queries fail."""
    pass
