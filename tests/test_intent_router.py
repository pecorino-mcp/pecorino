import pytest
from src.mcp_server.intent_router import IntentRouter

def test_intent_router():
    router = IntentRouter()
    
    # 1. Callers
    mode, q, intent = router.route("who calls _do_search")
    assert mode == "callers"
    assert q == "_do_search"
    assert intent is None
    
    mode, q, intent = router.route("callers of my_func")
    assert mode == "callers"
    assert q == "my_func"
    assert intent is None

    # 2. Callees
    mode, q, intent = router.route("what does my_func call?")
    assert mode == "callees"
    assert q == "my_func"
    assert intent is None
    
    mode, q, intent = router.route("callees of abc")
    assert mode == "callees"
    assert q == "abc"
    assert intent is None

    # 3. Impact
    mode, q, intent = router.route("impact of file.py")
    assert mode == "impact"
    assert q == "file.py"
    assert intent is None
    
    mode, q, intent = router.route("what depends on class_x")
    assert mode == "impact"
    assert q == "class_x"
    assert intent is None

    # 4. Community
    mode, q, intent = router.route("community my_symbol")
    assert mode == "community"
    assert q == "my_symbol"
    assert intent is None
    
    mode, q, intent = router.route("related to abc")
    assert mode == "community"
    assert q == "abc"
    assert intent is None

    # 5. Intent Overrides
    mode, q, intent = router.route("dead code")
    assert mode == "intent"
    assert intent == "dead_code"
    
    mode, q, intent = router.route("list classes")
    assert mode == "intent"
    assert intent == "all_classes"
    
    mode, q, intent = router.route("entry points")
    assert mode == "intent"
    assert intent == "entry_points"
    
    # 6. Cypher
    mode, q, intent = router.route("MATCH (n) RETURN n")
    assert mode == "cypher"
    assert intent is None

    # 7. Fallbacks
    mode, q, intent = router.route("how to implement login")
    assert mode == "hybrid"
    assert q == "how to implement login"
    assert intent is None
    
    mode, q, intent = router.route("")
    assert mode == "hybrid"
    assert q == ""
