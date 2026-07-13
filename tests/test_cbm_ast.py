import pytest
from src.parsers.tree_sitter_parser import parse_with_tree_sitter

def test_cbm_ast_extraction():
    code = """
import os

@app.get("/items/{item_id}")
def read_item(item_id: int):
    # Recursion check
    if item_id > 0:
        return read_item(item_id - 1)
        
    db_url = os.getenv("DATABASE_URL")
    api_key = os.environ["API_KEY"]
    
    if db_url is None:
        if api_key is None:
            raise ValueError("No database url or api key")
            
    return {"item_id": item_id, "db": db_url}
"""
    module = parse_with_tree_sitter(code, ".py")
    assert module is not None
    
    # 1. Routes
    assert len(module.routes) == 1
    route = module.routes[0]
    assert route.http_method == "GET"
    assert route.path == "/items/{item_id}"
    
    # 2. Env Vars
    env_names = {var.name for var in module.env_vars}
    assert "DATABASE_URL" in env_names
    assert "API_KEY" in env_names
    
    # 3. Functions / Methods
    assert len(module.functions) == 1
    func = module.functions[0]
    assert func.name == "read_item"
    assert func.is_recursive is True
    assert "ValueError" in func.raised_exceptions
    
    # 4. Cognitive Complexity
    # Nesting level check: 
    # if item_id > 0 (1 + 0 = 1)
    # if db_url is None (1 + 0 = 1)
    # if api_key is None (1 + 1 = 2)
    # Total = 4
    assert func.cognitive_complexity == 4
    assert func.is_test is False

    # 5. Test Functions
    test_code = """
def test_read_item():
    pass
"""
    test_module = parse_with_tree_sitter(test_code, ".py")
    assert test_module.functions[0].is_test is True

