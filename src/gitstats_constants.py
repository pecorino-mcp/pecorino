"""
Language constants and file extension definitions for Gitstats3.

This module centralizes language-specific constants used across the codebase,
including file extensions for analysis and language keywords for parsing.
"""

from typing import Set, Dict


# ============================================================================
# ALLOWED FILE EXTENSIONS
# ============================================================================
# These extensions are used by default for code analysis and metrics calculation.
# Users can override this via the `allowed_extensions` config option.

ALLOWED_EXTENSIONS: Set[str] = {
    # C/C++ family
    '.c', '.cc', '.cpp', '.cxx', '.h', '.hh', '.hpp', '.hxx',
    
    # Objective-C
    '.m', '.mm',
    
    # Apple Swift
    '.swift',
    
    # CUDA
    '.cu', '.cuh',
    
    # OpenCL
    '.cl',
    
    # Java/JVM languages
    '.java', '.scala', '.kt', '.kts',
    
    # Go
    '.go',
    
    # Rust
    '.rs',
    
    # Python
    '.py', '.pyi', '.pyx', '.pxd',
    
    # JavaScript/TypeScript
    '.js', '.mjs', '.cjs', '.jsx',
    '.ts', '.tsx', '.d.ts',
    
    # Lua
    '.lua',
    
    # Protocol Buffers / Thrift
    '.proto', '.thrift',
    
    # Assembly
    '.asm', '.s', '.S',
    
    # R language
    '.R', '.r',
    
    # Ruby
    '.rb', '.rake', '.gemspec',
    
    # PHP
    '.php', '.phtml',
    
    # C#
    '.cs',
    
    # Shell scripts
    '.sh', '.bash', '.zsh',
    
    # Perl
    '.pl', '.pm',
}


# ============================================================================
# LANGUAGE KEYWORDS
# ============================================================================
# Keywords for each supported programming language.
# Used by the tokenizer for syntax analysis.

LANGUAGE_KEYWORDS: Dict[str, Set[str]] = {
    'python': {
        # Control flow
        'if', 'else', 'elif', 'for', 'while', 'break', 'continue', 'return',
        'try', 'except', 'finally', 'raise', 'with', 'assert',
        # Definitions
        'class', 'def', 'lambda', 'async', 'await', 'yield',
        # Imports
        'import', 'from', 'as',
        # Scope
        'global', 'nonlocal',
        # Operators
        'and', 'or', 'not', 'in', 'is',
        # Values
        'True', 'False', 'None',
        # Other
        'pass', 'del',
    },
    
    'java': {
        # Control flow
        'if', 'else', 'for', 'while', 'do', 'switch', 'case', 'default',
        'break', 'continue', 'return',
        'try', 'catch', 'finally', 'throw', 'throws',
        # Definitions
        'class', 'interface', 'enum', 'abstract', 'final',
        'extends', 'implements', 'new',
        # Access modifiers
        'public', 'private', 'protected', 'static',
        # Other modifiers
        'synchronized', 'volatile', 'transient', 'native',
        # Import/Package
        'import', 'package',
        # References
        'this', 'super',
        # Values
        'void', 'null', 'true', 'false',
    },
    
    'javascript': {
        # Control flow
        'if', 'else', 'for', 'while', 'do', 'switch', 'case', 'default',
        'break', 'continue', 'return',
        'try', 'catch', 'finally', 'throw',
        # Definitions
        'function', 'class', 'extends', 'new',
        'const', 'let', 'var',
        'async', 'await', 'yield',
        # Import/Export
        'import', 'export', 'from', 'default',
        # References
        'this', 'super',
        # Operators
        'typeof', 'instanceof', 'delete', 'in',
        # Values
        'null', 'undefined', 'true', 'false', 'NaN', 'Infinity',
    },
    
    'typescript': {
        # Control flow (same as JS)
        'if', 'else', 'for', 'while', 'do', 'switch', 'case', 'default',
        'break', 'continue', 'return',
        'try', 'catch', 'finally', 'throw',
        # Definitions
        'function', 'class', 'interface', 'type', 'enum',
        'extends', 'implements', 'new', 'abstract',
        'const', 'let', 'var',
        'async', 'await',
        # Import/Export
        'import', 'export', 'from', 'default',
        # Access modifiers
        'public', 'private', 'protected', 'readonly', 'static',
        # References
        'this', 'super',
        # Type keywords
        'as', 'is', 'keyof', 'typeof', 'infer', 'never', 'unknown',
        # Values
        'null', 'undefined', 'true', 'false',
    },
    
    'cpp': {
        # Control flow
        'if', 'else', 'for', 'while', 'do', 'switch', 'case', 'default',
        'break', 'continue', 'return', 'goto',
        'try', 'catch', 'throw',
        # Definitions
        'class', 'struct', 'enum', 'union', 'namespace',
        'template', 'typename', 'typedef', 'using',
        'virtual', 'override', 'final', 'explicit', 'operator',
        'new', 'delete',
        # Access/Storage modifiers
        'public', 'private', 'protected', 'friend',
        'static', 'const', 'constexpr', 'mutable',
        'inline', 'extern', 'register', 'volatile',
        # References
        'this',
        # Operators
        'sizeof', 'alignof', 'decltype', 'noexcept',
        # Values
        'nullptr', 'true', 'false',
    },
    
    'go': {
        # Control flow
        'if', 'else', 'for', 'switch', 'case', 'default',
        'break', 'continue', 'return', 'goto', 'fallthrough',
        'select', 'defer',
        # Definitions
        'func', 'type', 'struct', 'interface',
        'package', 'import',
        'const', 'var',
        'map', 'chan',
        'go', 'range',
        # Values
        'nil', 'true', 'false', 'iota',
    },
    
    'rust': {
        # Control flow
        'if', 'else', 'for', 'while', 'loop', 'match',
        'break', 'continue', 'return',
        # Definitions
        'fn', 'struct', 'enum', 'trait', 'impl', 'type',
        'mod', 'use', 'crate',
        'const', 'static', 'let',
        'async', 'await', 'move',
        # Modifiers
        'pub', 'mut', 'ref', 'dyn', 'unsafe', 'extern',
        # References
        'self', 'Self', 'super',
        # Other
        'where', 'as', 'in',
    },
    
    'swift': {
        # Control flow
        'if', 'else', 'guard', 'switch', 'case', 'default',
        'for', 'while', 'repeat',
        'break', 'continue', 'return', 'fallthrough',
        'do', 'try', 'catch', 'throw', 'throws', 'rethrows',
        # Definitions
        'class', 'struct', 'enum', 'protocol', 'extension',
        'func', 'init', 'deinit', 'subscript',
        'typealias', 'associatedtype',
        'var', 'let',
        # Modifiers
        'public', 'private', 'internal', 'fileprivate', 'open',
        'static', 'final', 'override', 'mutating', 'lazy', 'weak',
        # Import
        'import',
        # References
        'self', 'Self', 'super',
        # Values
        'nil', 'true', 'false',
        # Other
        'where', 'as', 'is', 'in', 'inout',
    },
}


# ============================================================================
# FILE EXTENSION TO LANGUAGE MAPPING
# ============================================================================
# Maps file extensions to their corresponding language identifier.

EXTENSION_TO_LANGUAGE: Dict[str, str] = {
    # Python
    '.py': 'python', '.pyi': 'python', '.pyx': 'python', '.pxd': 'python',
    
    # JavaScript
    '.js': 'javascript', '.mjs': 'javascript', '.cjs': 'javascript', '.jsx': 'javascript',
    
    # TypeScript
    '.ts': 'typescript', '.tsx': 'typescript', '.d.ts': 'typescript',
    
    # Java
    '.java': 'java',
    
    # Kotlin
    '.kt': 'java', '.kts': 'java',  # Similar keywords
    
    # Scala
    '.scala': 'java',  # Similar keywords
    
    # C/C++
    '.c': 'cpp', '.cc': 'cpp', '.cpp': 'cpp', '.cxx': 'cpp',
    '.h': 'cpp', '.hh': 'cpp', '.hpp': 'cpp', '.hxx': 'cpp',
    
    # Go
    '.go': 'go',
    
    # Rust
    '.rs': 'rust',
    
    # Swift
    '.swift': 'swift',
}


# ============================================================================
# COMMON CONTROL FLOW PATTERNS
# ============================================================================
# These patterns are used for complexity analysis across languages.

CONTROL_FLOW_KEYWORDS: Set[str] = {
    # Conditionals
    'if', 'else', 'elif', 'elsif', 'elseif',
    'switch', 'case', 'default', 'match', 'when', 'guard',
    
    # Loops
    'for', 'while', 'do', 'loop', 'repeat', 'foreach',
    
    # Loop control
    'break', 'continue', 'fallthrough', 'goto',
    
    # Exception handling
    'try', 'catch', 'except', 'finally', 'throw', 'raise',
    
    # Return
    'return', 'yield',
}


# ============================================================================
# DECISION POINT PATTERNS (for cyclomatic complexity)
# ============================================================================
# These add to cyclomatic complexity when encountered.

DECISION_KEYWORDS: Set[str] = {
    'if', 'elif', 'elsif', 'elseif',
    'for', 'while', 'do',
    'case', 'catch', 'except',
    'and', 'or', '&&', '||',
    '?',  # Ternary operator
}


def get_language_for_extension(ext: str) -> str:
    """
    Get the language identifier for a file extension.
    
    Args:
        ext: File extension including the dot (e.g., '.py')
        
    Returns:
        Language identifier string, or 'unknown' if not recognized
    """
    return EXTENSION_TO_LANGUAGE.get(ext.lower(), 'unknown')


def get_keywords_for_language(language: str) -> Set[str]:
    """
    Get the set of keywords for a programming language.
    
    Args:
        language: Language identifier (e.g., 'python', 'java')
        
    Returns:
        Set of keyword strings for the language
    """
    return LANGUAGE_KEYWORDS.get(language, set())
