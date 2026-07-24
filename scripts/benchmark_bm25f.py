"""Benchmark script comparing Tantivy BM25F vs DuckDB FTS baseline on latency and candidate retrieval."""
import os
import random
import time
import tempfile

from src.mcp_server.index_db import CodeSearchIndex
from src.mcp_server.tantivy_search import TantivyIndex


def run_benchmark():
    words = [
        "connect", "database", "query", "execute", "parse", "validate", "transform",
        "render", "build", "compile", "deploy", "test", "mock", "config", "cache",
        "auth", "session", "token", "handler", "route", "middleware", "response",
        "request", "header", "body", "json", "xml", "schema", "migrate", "index"
    ]
    kinds = ["function", "method", "class", "variable", "interface"]

    random.seed(42)
    sample_nodes = []
    for i in range(1000):
        name = f"{'_'.join(random.sample(words, 2))}_{i}"
        kind = random.choice(kinds)
        filepath = f"src/{random.choice(words)}/{name}.py"
        summary = " ".join(random.sample(words, random.randint(3, 8)))
        body = f"def {name}(): " + " ".join(random.choices(words, k=random.randint(20, 80)))
        sample_nodes.append({
            "id": f"{filepath}::{name}::{i}",
            "name": name,
            "kind": kind,
            "filepath": filepath,
            "hcgs_summary": summary,
            "body_text": body,
            "start_line": 1,
            "end_line": 20,
        })

    with tempfile.TemporaryDirectory() as td:
        db_path = os.path.join(td, "test_bench_code_search.duckdb")
        tantivy_path = os.path.join(td, "test_bench_tantivy")

        search_index = CodeSearchIndex(db_path=db_path)
        search_index._ensure_graph = lambda: None  # Disable graph Cypher lookup for pure search latency benchmark
        search_index.index_nodes(sample_nodes)
        search_index.rebuild_fts()

        tantivy_idx = TantivyIndex(index_path=tantivy_path)
        tantivy_idx.build(sample_nodes, index_path=tantivy_path)

        queries = [
            "connect database",
            "parse query AST",
            "validate session token",
            "render json response",
            "auth middleware route",
        ]

        N = 50

        print(f"=== Benchmarking across {len(sample_nodes)} symbols ({N} iterations per query) ===")

        # Benchmark 1: DuckDB FTS (single match_bm25)
        t0 = time.perf_counter()
        for _ in range(N):
            for q in queries:
                search_index.search(q, limit=10, mode="fts")
        duckdb_lat = (time.perf_counter() - t0) * 1000 / (N * len(queries))

        # Force Tantivy search
        search_index._tantivy = tantivy_idx
        t0 = time.perf_counter()
        for _ in range(N):
            for q in queries:
                search_index.search(q, limit=10, mode="fts")
        tantivy_lat = (time.perf_counter() - t0) * 1000 / (N * len(queries))

        # Benchmark Tantivy standalone engine search alone
        t0 = time.perf_counter()
        for _ in range(N):
            for q in queries:
                tantivy_idx.search(q, limit=10)
        tantivy_raw_lat = (time.perf_counter() - t0) * 1000 / (N * len(queries))

        print(f"DuckDB FTS Latency (average query):  {duckdb_lat:.2f} ms")
        print(f"Tantivy BM25F Latency (in search()): {tantivy_lat:.2f} ms")
        print(f"Tantivy BM25F Engine Latency (raw):  {tantivy_raw_lat:.2f} ms")
        print(f"Tantivy speedup vs DuckDB FTS:       {duckdb_lat / tantivy_lat:.2f}x")

        search_index.close()


if __name__ == "__main__":
    run_benchmark()
