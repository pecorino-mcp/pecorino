from src.mcp_server.naming_analyzer import analyze_name

cases = [
    "userName", "UserName", "user_name", "MAX_BUFFER_SIZE", "user-name",
    "User-Name", "USER-NAME", "username", "USERNAME", "java.util.logging",
    "m_name", "_internalValue", "name_", "m_user_name", "kConstant", "IService", "TArray", "__init__"
]

for c in cases:
    print(f"{c}: {analyze_name(c)}")
