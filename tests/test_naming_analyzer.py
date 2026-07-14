import pytest
from src.mcp_server.naming_analyzer import analyze_name

def test_analyze_name():
    cases = {
        "userName": {"case_style": "camelCase", "prefix": "", "suffix": "", "is_magic": False},
        "UserName": {"case_style": "PascalCase", "prefix": "", "suffix": "", "is_magic": False},
        "user_name": {"case_style": "snake_case", "prefix": "", "suffix": "", "is_magic": False},
        "MAX_BUFFER_SIZE": {"case_style": "SCREAMING_SNAKE_CASE", "prefix": "", "suffix": "", "is_magic": False},
        "user-name": {"case_style": "kebab-case", "prefix": "", "suffix": "", "is_magic": False},
        "User-Name": {"case_style": "Train-Case", "prefix": "", "suffix": "", "is_magic": False},
        "USER-NAME": {"case_style": "COBOL-CASE", "prefix": "", "suffix": "", "is_magic": False},
        "username": {"case_style": "flatcase", "prefix": "", "suffix": "", "is_magic": False},
        "USERNAME": {"case_style": "UPPERFLATCASE", "prefix": "", "suffix": "", "is_magic": False},
        "java.util.logging": {"case_style": "dot.case", "prefix": "", "suffix": "", "is_magic": False},
        "m_name": {"case_style": "flatcase", "prefix": "m_", "suffix": "", "is_magic": False},
        "_internalValue": {"case_style": "camelCase", "prefix": "_", "suffix": "", "is_magic": False},
        "name_": {"case_style": "flatcase", "prefix": "", "suffix": "_", "is_magic": False},
        "m_user_name": {"case_style": "snake_case", "prefix": "m_", "suffix": "", "is_magic": False},
        "kConstant": {"case_style": "PascalCase", "prefix": "k", "suffix": "", "is_magic": False},
        "IService": {"case_style": "PascalCase", "prefix": "I", "suffix": "", "is_magic": False},
        "TArray": {"case_style": "PascalCase", "prefix": "T", "suffix": "", "is_magic": False},
        "__init__": {"case_style": "flatcase", "prefix": "__", "suffix": "__", "is_magic": True},
    }
    for name, expected in cases.items():
        assert analyze_name(name) == expected
