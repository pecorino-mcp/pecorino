import os
import sys
import time
import psutil
import threading
from concurrent.futures import ThreadPoolExecutor
import tempfile
import random

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from src.mcp_server.index_pipeline import CodebaseIndexer
from src.mcp_server.index_db import CodeSearchIndex

DB_PATH = "stress_test_6gb.sqlite"
REPO_PATH = tempfile.mkdtemp()

WORDS = ["def", "class", "async", "await", "import", "from", "return", "yield", "Foo", "Bar", "Baz", "Qux", "main", "init", "setup", "teardown", "execute", "run", "process", "handle", "compute", "calculate", "validate", "verify", "authenticate", "authorize", "login", "logout", "register", "update", "delete", "insert", "select", "query", "fetch", "get", "set", "add", "remove", "append", "pop", "push", "shift", "unshift", "splice", "slice", "split", "join", "map", "filter", "reduce", "sort", "reverse", "find", "some", "every", "includes", "indexOf", "lastIndexOf", "charAt", "charCodeAt", "concat", "match", "replace", "search", "slice", "split", "substr", "substring", "toLowerCase", "toUpperCase", "trim"]

def generate_random_code():
    code = ""
    for _ in range(random.randint(10, 50)):
        line = " ".join(random.choices(WORDS, k=random.randint(5, 15)))
        code += line + "\n"
    return code

def monitor_memory(pid, stop_event):
    process = psutil.Process(pid)
    print("Starting memory monitor...")
    while not stop_event.is_set():
        mem_info = process.memory_info()
        print(f"[Memory] RSS: {mem_info.rss / 1024 / 1024:.2f} MB")
        time.sleep(2)

def search_worker(index, query, results_count, latencies):
    start = time.time()
    try:
        res = index.search(query, limit=10)
        results_count.append(len(res))
    except Exception as e:
        print(f"Search error: {e}")
    latencies.append(time.time() - start)

def main():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        
    pid = os.getpid()
    stop_event = threading.Event()
    monitor_thread = threading.Thread(target=monitor_memory, args=(pid, stop_event))
    monitor_thread.start()
    
    print("Starting continuous inserts and searches for 60 seconds...")
    start_time = time.time()
    
    indexer = CodebaseIndexer(repo_path=REPO_PATH)
    indexer.search_index.rebuild_fts() # Force init
    
    search_index = CodeSearchIndex(db_path=indexer.search_index.db_path, read_only=False)
    
    total_inserts = 0
    total_searches = 0
    search_latencies = []
    search_results = []
    
    executor = ThreadPoolExecutor(max_workers=10)
    
    try:
        while time.time() - start_time < 60:
            # Insert a batch of 100 documents
            for i in range(100):
                filepath = os.path.join(REPO_PATH, f"file_{total_inserts}.py")
                code = generate_random_code()
                do_rebuild = (total_inserts > 0 and total_inserts % 1000 == 0)
                indexer.index_file(filepath, code, ".py", rebuild_fts=do_rebuild)
                total_inserts += 1
            
            # Fire off 10 concurrent searches
            futures = []
            for _ in range(10):
                query = random.choice(WORDS)
                futures.append(executor.submit(search_worker, search_index, query, search_results, search_latencies))
                total_searches += 1
                
            for f in futures:
                f.result()
                
            time.sleep(0.1) # Small pause to let background workers catch up
            
    finally:
        stop_event.set()
        monitor_thread.join()
        indexer.close()
        search_index.close()
        
    print("\n--- Stress Test Results ---")
    print(f"Total inserts: {total_inserts}")
    print(f"Total searches: {total_searches}")
    if search_latencies:
        avg_lat = sum(search_latencies) / len(search_latencies)
        max_lat = max(search_latencies)
        print(f"Average search latency: {avg_lat * 1000:.2f} ms")
        print(f"Max search latency: {max_lat * 1000:.2f} ms")
        print(f"Average results per search: {sum(search_results) / len(search_results):.2f}")

if __name__ == "__main__":
    main()
