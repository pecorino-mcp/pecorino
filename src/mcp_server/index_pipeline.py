import gc
import hashlib
import json
import logging
import os
import pathlib
import shutil
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

# Add modules/pecorino-utils to sys.path to resolve src.metrics
workspace_root = Path(__file__).resolve().parent.parent.parent
utils_path = workspace_root / "modules" / "pecorino-utils"
if str(utils_path) not in sys.path:
    sys.path.insert(0, str(utils_path))
if str(workspace_root) not in sys.path:
    sys.path.insert(0, str(workspace_root))

from src.core.constants import SUPPORTED_EXTENSIONS, get_language_for_extension
from src.mcp_server.config import settings
from src.mcp_server.index_db import CodeSearchIndex, find_repo_root, get_db_path_for_repo
from src.mcp_server.indexing.dependency_resolver import (
    build_repo_file_cache,
    find_file_in_repo,
    resolve_dependency,
    resolve_relative_fallback,
    walk_repo_files,
)
from src.mcp_server.indexing.git_analytics import (
    compute_git_coupling,
    compute_git_features,
)
from src.mcp_server.indexing.graph_post_processor import (
    compute_similarity_edges,
    post_process_graph,
    run_hcgs_pass,
    verify_index_integrity,
)
from src.mcp_server.indexing.lsp_resolver import (
    find_lsp_resolutions,
    link_lsp_call_edges,
)
from src.mcp_server.indexing.profiler import IndexProfiler
from src.mcp_server.naming_analyzer import analyze_name
from src.mcp_server.ramdisk import RamdiskIndex

logger = logging.getLogger(__name__)


def _attach_identifier_nodes(
    graph_nodes: list,
    graph_edges: list,
    filepath: str,
) -> tuple[list, list]:
    """Build Identifier nodes and HAS_IDENTIFIER edges for graph nodes."""
    final_graph_nodes = []
    identifier_nodes_dict = {}
    for nid, props, lbl in graph_nodes:
        if "name" in props and props["name"]:
            raw_name = props["name"]
            if lbl == "File":
                raw_name = Path(raw_name).stem

            ident_id = raw_name
            if ident_id not in identifier_nodes_dict:
                analysis = analyze_name(raw_name, filepath)
                identifier_nodes_dict[ident_id] = (
                    ident_id,
                    {
                        "raw": raw_name,
                        **analysis,
                    },
                    "Identifier",
                )
            else:
                analysis = identifier_nodes_dict[ident_id][1]

            props.setdefault("canonical_verb", analysis.get("canonical_verb", ""))
            graph_edges.append((nid, ident_id, {}, "HAS_IDENTIFIER"))
        final_graph_nodes.append((nid, props, lbl))

    if identifier_nodes_dict:
        final_graph_nodes.extend(identifier_nodes_dict.values())

    return final_graph_nodes, graph_edges


class CodebaseIndexer:
    def __init__(self, repo_path: str = None):
        repo_path = repo_path if repo_path else find_repo_root(os.getcwd())

        from src.mcp_server.config import settings
        from src.mcp_server.index_db import CodeSearchIndex

        self.repo_path = repo_path
        db_path = get_db_path_for_repo(repo_path)
        self.search_index = CodeSearchIndex(db_path=db_path)
        self.graph = self.search_index._ensure_graph()

        self.enable_embeddings = getattr(settings, 'enable_embeddings', True)
        if self.enable_embeddings:
            from src.mcp_server.embedder import Embedder
            self.embedder = Embedder(self.search_index._conn)
        else:
            self.embedder = None
        self.enable_lsp = settings.enable_lsp
        self._repo_cache_lock = threading.Lock()

    def close(self):
        """Release the underlying database and Gorgonzola connections."""
        if self.search_index is not None:
            self.search_index.close()
            self.search_index = None
        if self.graph is not None:
            if hasattr(self.graph, 'close'):
                try:
                    self.graph.close()
                except Exception as e:
                    logger.debug("Error closing graph: %s", e)
            self.graph = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def _rebind_embedder(self, db_conn) -> None:
        if self.embedder:
            self.embedder.db_conn = db_conn
            if hasattr(self.embedder, "_ensure_cache_table"):
                try:
                    self.embedder._ensure_cache_table()
                except Exception as e:
                    logger.debug("Failed to ensure embeddings_cache table: %s", e)

    def _find_file_in_repo(self, dep_string: str) -> str:
        """Find a file in the repo that matches dep_string (e.g. ending with it)."""
        with self._repo_cache_lock:
            if not hasattr(self, '_repo_files_cache') or not hasattr(self, '_repo_basename_index'):
                self._repo_files_cache, self._repo_basename_index = build_repo_file_cache(self.repo_path)
        return find_file_in_repo(dep_string, self.repo_path, self._repo_basename_index)

    def _resolve_relative_fallback(self, dep_string: str, source_filepath: str) -> str:
        """Shared fallback: resolve a relative dep_string against the source file's directory."""
        return resolve_relative_fallback(dep_string, source_filepath)

    def _resolve_dependency(self, dep_string: str, source_filepath: str, file_extension: str) -> str | None:
        """Enhanced language-specific dependency file resolution."""
        return resolve_dependency(
            dep_string,
            source_filepath,
            file_extension,
            self.repo_path,
            find_file_fn=self._find_file_in_repo,
        )

    def _extract_records(self, content: str, filepath: str, file_extension: str) -> dict:
        if file_extension == ".md" and "docs/adr" in filepath.replace("\\", "/"):
            # Simple markdown parsing for ADRs
            first_line = content.splitlines()[0] if content else "Untitled"
            title = first_line.lstrip('# ') if first_line.startswith('#') else "Untitled"
            nodes_to_index = [{
                "id": filepath,
                "name": title,
                "filepath": filepath,
                "signature": "",
                "body_text": content,
                "start_line": 1,
                "end_line": len(content.splitlines()) or 1,
                "kind": "ADR",
                "metrics": {}
            }]
            graph_nodes_dict = {
                filepath: (filepath, {
                    "name": title,
                    "path": filepath,
                    "extension": ".md",
                    "lang": "markdown"
                }, "ADR")
            }
            return {
                "nodes_to_index": nodes_to_index,
                "graph_nodes": list(graph_nodes_dict.values()),
                "graph_edges": [],
                "resolved_deps": []
            }

        # Tree-sitter extraction
        from src.mcp_server.ast.extractor import TreeSitterExtractor
        from src.parsers.tree_sitter_parser import get_raw_tree_sitter_tree

        tree = get_raw_tree_sitter_tree(content, file_extension)
        if not tree:
            return None

        def resolve_import_cb(import_text, current_file, project_root):
            return self._resolve_dependency(import_text, current_file, file_extension)

        extractor = TreeSitterExtractor(filepath, self.repo_path, resolve_import_cb)
        nodes_dict, edges_dict = extractor.extract(tree, content.encode('utf-8'))

        # Map to expected output format
        graph_nodes = []
        for n_id, n_props in nodes_dict.items():
            props = {
                "name": n_props.get("name"),
                "qualified_name": n_props.get("qualified_name"),
                "file": n_props.get("file"),
                "line": n_props.get("line"),
                "end_line": n_props.get("end_line"),
                "complexity": n_props.get("complexity"),
                "docstring": n_props.get("docstring", ""),
            }
            if n_props["kind"] == "File":
                props["path"] = n_props.get("file")
                props["lang"] = get_language_for_extension(file_extension)
            graph_nodes.append((n_id, props, n_props["kind"]))

        graph_edges = []
        for rel_type, rel_list in edges_dict.items():
            for edge in rel_list:
                src, dst, props = edge
                graph_edges.append((src, dst, props, rel_type))

        nodes_to_index = []
        for n_id, n_props in nodes_dict.items():
            if n_props["kind"] in ("Function", "Method", "Class"):
                nodes_to_index.append({
                    "id": n_id,
                    "name": n_props["name"],
                    "kind": n_props["kind"].lower(),
                    "filepath": n_props["file"],
                    "start_line": n_props["line"],
                    "end_line": n_props["end_line"],
                    "metrics": {"complexity": n_props.get("complexity", 1)},
                    "relationships": "",
                    "start_byte": n_props.get("start_byte", 0),
                    "end_byte": n_props.get("end_byte", 0)
                })

        return {
            "nodes_to_index": nodes_to_index,
            "graph_nodes": graph_nodes,
            "graph_edges": graph_edges,
            "resolved_deps": []
        }

    def index_file(
        self,
        filepath: str,
        content: str,
        file_extension: str,
        rebuild_fts: bool = False,
        update_file_hash: bool = True,
    ):
        """Parse and index a single file for search and dependency graph."""
        MAX_FILE_SIZE = 2 * 1024 * 1024
        if len(content.encode('utf-8', errors='ignore')) > MAX_FILE_SIZE:
            logger.warning("Skipping %s (exceeds 2MB limit)", filepath)
            return

        records = self._extract_records(content, filepath, file_extension)
        if not records:
            return

        self._embed_nodes(records.get("nodes_to_index", []), content.encode('utf-8'))

        self.search_index.clear_file(filepath)

        nodes_to_index = records.get("nodes_to_index", [])
        graph_nodes = records.get("graph_nodes", [])
        graph_edges = records.get("graph_edges", [])

        if nodes_to_index:
            self.search_index.index_nodes(nodes_to_index)

        final_graph_nodes, graph_edges = _attach_identifier_nodes(
            graph_nodes, graph_edges, filepath
        )

        # Embeddings for graph nodes have been moved to _index_directory_impl for global batching

        if final_graph_nodes:
            try:
                id_map = self.graph.insert_nodes_bulk(final_graph_nodes)
                if graph_edges:
                    self.graph.insert_edges_bulk(graph_edges, id_map)
            except Exception as e:
                logger.warning("Failed to insert graph nodes/edges for %s: %s", filepath, e)
                logger.debug(traceback.format_exc())

        if update_file_hash and os.path.exists(filepath):
            try:
                mtime = os.path.getmtime(filepath)
                content_hash = hashlib.md5(content.encode('utf-8')).hexdigest()
                lang = get_language_for_extension(file_extension)
                self.search_index.upsert_file_hash(filepath, content_hash, mtime, lang)
            except Exception as e:
                logger.debug("Failed to upsert file hash for %s: %s", filepath, e)

        if rebuild_fts:
            try:
                self.search_index.rebuild_fts()
            except Exception as e:
                logger.warning("Failed to rebuild FTS index for %s: %s", filepath, e)
                logger.debug(traceback.format_exc())
        else:
            self.search_index.mark_fts_dirty()

    def _parse_file_task(self, fp: Any, file_str: str, mtime: float):
        try:
            MAX_FILE_SIZE = 2 * 1024 * 1024
            if fp.stat().st_size > MAX_FILE_SIZE:
                return None

            content = fp.read_text(encoding='utf-8', errors='ignore')
            content_hash = hashlib.md5(content.encode('utf-8')).hexdigest()

            if getattr(self, "lsp_client", None):
                try:
                    self.lsp_client.open_document(file_str, content)
                except Exception as e:
                    logger.debug("Failed to open document in LSP: %s", e)

            records = self._extract_records(content, file_str, fp.suffix)
            if not records:
                return None

            content_bytes = content.encode('utf-8')

            lsp_resolutions = []
            if getattr(self, "lsp_client", None):
                try:
                    from src.parsers.tree_sitter_parser import get_raw_tree_sitter_tree
                    raw_tree = get_raw_tree_sitter_tree(content, fp.suffix)
                    if raw_tree:
                        lsp_resolutions = self._find_lsp_resolutions(raw_tree, file_str)
                except Exception as e:
                    logger.debug("Failed to resolve definitions via LSP: %s", e)

            records.update({
                "file_str": file_str,
                "content_hash": content_hash,
                "mtime": mtime,
                "lang": fp.suffix,
                "content_bytes": content_bytes,
                "lsp_resolutions": lsp_resolutions
            })
            return records
        except Exception as e:
            logger.warning("Failed to parse %s: %s", file_str, e)
            logger.debug(traceback.format_exc())
            return None

    def _embed_nodes(self, nodes_to_index: list, content_bytes: bytes):
        if not self.enable_embeddings or not self.embedder:
            return
        if not nodes_to_index:
            return

        texts_to_embed = []
        for n in nodes_to_index:
            s_byte = n.get('start_byte', 0)
            e_byte = n.get('end_byte', 0)
            if e_byte > s_byte:
                try:
                    text = content_bytes[s_byte:e_byte].decode('utf-8', errors='ignore')
                except Exception:
                    text = ""
            else:
                text = ""
            texts_to_embed.append(text)

        embeddings = self.embedder.embed_texts(texts_to_embed)
        for i, n in enumerate(nodes_to_index):
            if i < len(embeddings):
                n['embedding'] = embeddings[i]

    def _compute_git_features(self, dirpath: str) -> list:
        """Parse git log to extract per-file temporal and stable features."""
        return compute_git_features(dirpath)

    def _compute_git_coupling(self, dirpath: str) -> list:
        """Run git log to calculate file co-change coupling scores (Jaccard similarity)."""
        return compute_git_coupling(dirpath)

    def _find_lsp_resolutions(self, tree, file_str) -> list:
        return find_lsp_resolutions(getattr(self, "lsp_client", None), tree, file_str)

    def _compute_similarity_edges(self):
        """Compute SIMILAR_TO (MinHash/Jaccard) and SEMANTICALLY_RELATED (Vector) edges."""
        compute_similarity_edges(self.search_index, self.graph, enable_embeddings=self.enable_embeddings)

    def _post_process_graph(self):
        """Find recursive self-calls and resolve Symbol nodes to Method/Function for dynamic languages."""
        post_process_graph(
            self.search_index,
            self.graph,
            self.repo_path,
            enable_embeddings=self.enable_embeddings,
            compute_git_features_fn=self._compute_git_features,
        )

    def _verify_index_integrity(self) -> dict:
        """Post-indexing sanity check: verify SQLite ↔ Gorgonzola consistency."""
        return verify_index_integrity(self.search_index, self.graph)

    def index_directory(self, dirpath: str, progress_callback=None) -> dict:
        path = pathlib.Path(dirpath).resolve()
        self.lsp_client = None
        if self.enable_lsp:
            try:
                from src.mcp_server.config import settings
                from src.mcp_server.lsp.manager import LSPClientPool
                self.lsp_client = LSPClientPool(workspace_root=str(path), pool_size=settings.lsp_pool_size)
                if not self.lsp_client.start():
                    self.lsp_client = None
            except Exception as e:
                logger.warning("Failed to start LSP client pool: %s", e)
                self.lsp_client = None
        try:
            return self._index_directory_impl(dirpath, progress_callback)
        finally:
            if self.lsp_client:
                self.lsp_client.stop()
                self.lsp_client = None

    def _index_directory_impl(self, dirpath: str, progress_callback=None) -> dict:
        profiler = IndexProfiler()
        path = pathlib.Path(dirpath).resolve()
        files = walk_repo_files(path, SUPPORTED_EXTENSIONS)

        total_files = len(files)
        try:
            total_source_bytes = sum(fp.stat().st_size for fp in files)
        except Exception:
            total_source_bytes = 0

        projected_db_bytes = int(total_source_bytes * 40.0)
        required_ramdisk_bytes = max(int(projected_db_bytes * 1.5), 100 * 1024 * 1024)

        if progress_callback:
            progress_callback(0, total_files, f"Projected raw DB size: {projected_db_bytes / (1024*1024):.1f} MB")

        # Get existing metadata to avoid concurrent DB queries in threads
        tracked_metadata = {}
        try:
            if self.search_index and self.search_index._conn:
                rows = self.search_index._conn.execute('SELECT filepath, content_hash, mtime FROM files').fetchall()
                tracked_metadata = {row[0]: (row[1], row[2]) for row in rows}
        except Exception as e:
            logger.debug("Failed to get tracked metadata: %s", e)

        indexed_count = 0
        skipped_count = 0
        current_files_set = set()
        parse_jobs = []

        for fp in files:
            file_str = str(fp)
            current_files_set.add(file_str)
            try:
                mtime = os.path.getmtime(file_str)
                if file_str in tracked_metadata:
                    existing_hash, existing_mtime = tracked_metadata[file_str]
                    if abs(mtime - existing_mtime) < 0.01:
                        skipped_count += 1
                        continue
                parse_jobs.append((fp, file_str, mtime))
            except Exception as e:
                logger.warning("Failed to stat %s: %s", file_str, e)

        # Close existing connections before mass update/ramdisk to prevent connection errors
        ssd_db_path = self.search_index.db_path
        self.close()

        if not parse_jobs:
            self.search_index = CodeSearchIndex(ssd_db_path)
            self.graph = self.search_index.graph
            self._rebind_embedder(self.search_index._conn)

            tracked_files = self.search_index.get_all_tracked_files()
            target_prefix = str(path) + os.sep
            stale_files = [tf for tf in tracked_files if (tf == str(path) or tf.startswith(target_prefix)) and tf not in current_files_set]
            stale_count = len(stale_files)
            if stale_files:
                if progress_callback:
                    progress_callback(total_files, total_files, f"Removing {stale_count} stale files...")
                with self.graph:
                    self.search_index.clear_files_bulk(stale_files)

            fts_error = None
            if not self.search_index.has_fts_index() or self.search_index.is_fts_dirty():
                try:
                    self.search_index.rebuild_fts()
                except Exception as e:
                    fts_error = str(e)
                    logger.warning("Failed to rebuild FTS index: %s", fts_error)

            res = {
                "status": "success" if not fts_error else "partial",
                "indexed_files": 0,
                "skipped_files": skipped_count,
                "stale_files_removed": stale_count,
                "total_files_found": total_files
            }
            if fts_error:
                res["fts_error"] = fts_error
            return res

        class DummyContext:
            def __init__(self, db_path):
                self.db_path = db_path
            def __enter__(self): return self
            def __exit__(self, exc_type, exc_val, exc_tb): return False
            def check_quota(self): pass
            def get_usage_bytes(self): return 0

        # Ramdisk Fallback Check
        use_ramdisk = os.getenv("PECORINO_USE_RAMDISK", "true").lower() not in ("false", "0", "no")
        if use_ramdisk:
            try:
                if os.path.exists('/dev/shm') and os.path.isdir('/dev/shm'):
                    shm_free = shutil.disk_usage('/dev/shm').free
                    if required_ramdisk_bytes > (shm_free - 50 * 1024 * 1024):
                        use_ramdisk = False
                        logger.info("[ramdisk] Not enough /dev/shm (Free: %.1fMB, Req: %.1fMB), using SSD directly", shm_free/(1024*1024), required_ramdisk_bytes/(1024*1024))
                else:
                    use_ramdisk = False
            except Exception:
                use_ramdisk = False

        ramdisk = RamdiskIndex(ssd_db_path, max_bytes=required_ramdisk_bytes) if use_ramdisk else DummyContext(ssd_db_path)

        indexed_count = 0
        all_lsp_resolutions = []  # Phase 1 collection: (file_str, lsp_resolutions)

        with ramdisk:
            ram_search = CodeSearchIndex(ramdisk.db_path)
            ram_graph = ram_search.graph
            self._rebind_embedder(ram_search._conn)

            with ram_graph:
                max_workers = settings.index_max_workers
                CHUNK_SIZE = settings.index_chunk_size
                chunk_jobs = [parse_jobs[i:i + CHUNK_SIZE] for i in range(0, len(parse_jobs), CHUNK_SIZE)]
                current_processed = skipped_count

                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    for _chunk_idx, chunk in enumerate(chunk_jobs):
                        chunk_results = []
                        futures = {executor.submit(self._parse_file_task, job[0], job[1], job[2]): job[1] for job in chunk}

                        for fut in as_completed(futures):
                            file_str = futures[fut]
                            try:
                                res = fut.result()
                                if res:
                                    existing_hash = tracked_metadata.get(file_str, (None, None))[0]
                                    if existing_hash == res["content_hash"]:
                                        skipped_count += 1
                                    else:
                                        chunk_results.append(res)
                            except Exception as e:
                                logger.warning("Task failed for %s: %s", file_str, e)

                            current_processed += 1
                            if progress_callback:
                                progress_callback(current_processed, total_files, f"Parsed {file_str}")

                        if not chunk_results:
                            continue

                        # Attach Identifier nodes and HAS_IDENTIFIER edges before embedding
                        for res in chunk_results:
                            res["graph_nodes"], res["graph_edges"] = _attach_identifier_nodes(
                                res.get("graph_nodes", []),
                                res.get("graph_edges", []),
                                res["file_str"],
                            )

                        # 1. Embed search nodes and graph nodes for this chunk only
                        with profiler.profile("Vector Embeddings Generation"):
                            if self.enable_embeddings and self.embedder:
                                all_nodes_to_embed = []
                                texts_to_embed = []
                                for res in chunk_results:
                                    content_bytes = res.get("content_bytes", b"")
                                    for n in res.get("nodes_to_index", []):
                                        s_byte = n.get('start_byte', 0)
                                        e_byte = n.get('end_byte', 0)
                                        if e_byte > s_byte:
                                            try:
                                                text = content_bytes[s_byte:e_byte].decode('utf-8', errors='ignore')
                                            except Exception:
                                                text = ""
                                        else:
                                            text = ""
                                        if text.strip():
                                            texts_to_embed.append(text)
                                            all_nodes_to_embed.append(n)

                                if texts_to_embed:
                                    try:
                                        embeddings = self.embedder.embed_texts(texts_to_embed)
                                        for i, n in enumerate(all_nodes_to_embed):
                                            if i < len(embeddings):
                                                n['embedding'] = embeddings[i]
                                    except Exception:
                                        logger.debug(traceback.format_exc())

                                all_graph_nodes_to_embed = []
                                graph_texts_to_embed = []
                                seen_embed_nids = set()
                                for res in chunk_results:
                                    for nid, props, lbl in res.get("graph_nodes", []):
                                        if nid in seen_embed_nids:
                                            continue
                                        if lbl in ("External", "ExternalType", "File", "Parameter"):
                                            continue
                                        if lbl == "Identifier":
                                            text = f"{props.get('raw', '')} {props.get('canonical_verb', '')} {props.get('canonical_entity', '')}"
                                        else:
                                            name = props.get("name", "")
                                            doc = props.get("docstring", "")
                                            fp = nid.split("::")[0] if "::" in nid else ""
                                            cv = props.get("canonical_verb", "")
                                            text = f"{name} {doc} {fp} {cv}"
                                        if text.strip():
                                            seen_embed_nids.add(nid)
                                            graph_texts_to_embed.append(text)
                                            all_graph_nodes_to_embed.append(props)

                                if graph_texts_to_embed:
                                    try:
                                        graph_embeddings = self.embedder.embed_texts(graph_texts_to_embed)
                                        for i, props in enumerate(all_graph_nodes_to_embed):
                                            if i < len(graph_embeddings):
                                                props["embedding"] = graph_embeddings[i]
                                    except Exception as e:
                                        logger.warning("Failed vector embeddings for chunk graph nodes: %s", e)
                                        logger.debug(traceback.format_exc())

                        # 2. Clear existing indexes for modified files in this chunk
                        with profiler.profile("Bulk Database Ingestion"):
                            files_to_clear = [res["file_str"] for res in chunk_results]
                            if files_to_clear:
                                ram_search.clear_files_bulk(files_to_clear)

                            # 3. Save search nodes and graph nodes for this chunk
                            chunk_search_nodes = []
                            chunk_graph_nodes = {}
                            chunk_graph_edges = set()
                            chunk_metadata = []

                            for res in chunk_results:
                                file_str = res["file_str"]
                                if res["nodes_to_index"]:
                                    chunk_search_nodes.extend(res["nodes_to_index"])
                                if res["graph_nodes"]:
                                    for node_id, props, lbl in res["graph_nodes"]:
                                        if node_id not in chunk_graph_nodes or "embedding" in props:
                                            chunk_graph_nodes[node_id] = (props, lbl)
                                if res["graph_edges"]:
                                    for src, dst, props, rel in res["graph_edges"]:
                                        chunk_graph_edges.add((src, dst, frozenset(props.items()), rel))

                                if res.get("lsp_resolutions"):
                                    all_lsp_resolutions.append((file_str, res["lsp_resolutions"]))

                                lang_name = get_language_for_extension(res["lang"])
                                chunk_metadata.append((file_str, res["content_hash"], res["mtime"], lang_name))

                            if chunk_search_nodes:
                                ram_search.index_nodes(chunk_search_nodes)

                            if chunk_graph_nodes:
                                nodes_list = [
                                    (nid, props, lbl)
                                    for nid, (props, lbl) in chunk_graph_nodes.items()
                                ]
                                id_map = ram_graph.insert_nodes_bulk(nodes_list)

                                if chunk_graph_edges:
                                    edges_list = [(src, dst, dict(props), rel) for src, dst, props, rel in chunk_graph_edges]
                                    ram_graph.insert_edges_bulk(edges_list, id_map)

                            if chunk_metadata:
                                ram_search.upsert_file_hashes_bulk(chunk_metadata)

                        indexed_count += len(chunk_results)

                        # Clear chunk memory and run garbage collection
                        del chunk_results, chunk_search_nodes, chunk_graph_nodes, chunk_graph_edges
                        gc.collect()

                # Phase 2: Execute LSP definition resolution across all collected instructions
                with profiler.profile("LSP Call Resolution"):
                    if all_lsp_resolutions:
                        if progress_callback:
                            progress_callback(total_files, total_files, "Resolving cross-file LSP call edges...")
                        link_lsp_call_edges(ram_search, ram_graph, all_lsp_resolutions)

                # Insert Git temporal coupling edges
                with profiler.profile("Git Temporal Coupling"):
                    try:
                        ram_graph.query_batch(["MATCH ()-[r:FILE_CHANGES_WITH]->() DELETE r"])
                    except Exception as e:
                        logger.debug("Failed to clear old FILE_CHANGES_WITH edges: %s", e)

                    git_coupling = self._compute_git_coupling(str(path))
                    if git_coupling:
                        if progress_callback:
                            progress_callback(total_files, total_files, f"Linking {len(git_coupling)} git temporal coupling edges...")
                    git_edges = []
                    for f1, f2, weight in git_coupling:
                        lbl1 = ram_graph._get_node_label(f1, ram_graph._conn)
                        lbl2 = ram_graph._get_node_label(f2, ram_graph._conn)
                        if lbl1 == "File" and lbl2 == "File":
                            git_edges.append((f1, f2, {"weight": weight}, "FILE_CHANGES_WITH"))
                    if git_edges:
                        ram_graph.insert_edges_bulk(git_edges)

                ramdisk.check_quota()
            ram_search.close()

        self.search_index = CodeSearchIndex(ssd_db_path)
        self.graph = self.search_index.graph
        self._rebind_embedder(self.search_index._conn)

        tracked_files = self.search_index.get_all_tracked_files()
        target_prefix = str(path) + os.sep
        stale_files = [tf for tf in tracked_files if (tf == str(path) or tf.startswith(target_prefix)) and tf not in current_files_set]
        stale_count = len(stale_files)
        if stale_files:
            if progress_callback:
                progress_callback(total_files, total_files, f"Removing {stale_count} stale files...")
            with self.graph:
                self.search_index.clear_files_bulk(stale_files)

        with profiler.profile("Graph OOD Algorithms"):
            if settings.enable_ood:
                self._post_process_graph()
            else:
                logger.info("OOD features disabled (PECORINO_ENABLE_OOD=false), skipping graph sweep.")

        with profiler.profile("SQLite FTS Rebuild"):
            fts_error = None
            try:
                if progress_callback:
                    progress_callback(total_files, total_files, "Rebuilding FTS index...")
                self.search_index.rebuild_fts()
            except Exception as e:
                fts_error = str(e)
                logger.warning("Failed to rebuild FTS index: %s", fts_error)
                logger.debug(traceback.format_exc())

        if progress_callback:
            progress_callback(total_files, total_files, "Resolving symbols...")

        from src.mcp_server.graph_api import GraphAPI
        graph_api = GraphAPI(self.repo_path, graph=self.graph)
        try:
            graph_api.resolve_symbols()
        finally:
            graph_api.close()

        # Static HCGS Pass (Zero-LLM context propagation & re-embedding)
        if settings.enable_hcgs:
            run_hcgs_pass(
                self.graph,
                self.search_index,
                embedder=self.embedder,
                enable_embeddings=self.enable_embeddings,
                progress_callback=progress_callback,
                total_files=total_files,
            )

        # Post-indexing integrity verification
        integrity_stats = self._verify_index_integrity()

        # Force Kuzu to checkpoint WAL to disk immediately, avoiding slow startups for subsequent query clients
        if getattr(self, "graph", None):
            try:
                with self.graph:
                    self.graph._conn.execute("CHECKPOINT;")
                    logger.info("Gorgonzola WAL checkpointed successfully.")
            except Exception as e:
                logger.warning("Failed to checkpoint Gorgonzola WAL: %s", e)

        self.search_index.close()

        res = {
            "status": "success" if not fts_error else "partial",
            "indexed_files": indexed_count,
            "skipped_files": skipped_count,
            "stale_files_removed": stale_count,
            "total_files_found": total_files
        }
        if fts_error:
            res["fts_error"] = fts_error
        if integrity_stats.get("warnings"):
            res["integrity_warnings"] = integrity_stats["warnings"]

        profiler.print_summary()
        return res


def progress_callback(current: int, total: int, file_path: str):
    print(json.dumps({
        "current": current,
        "total": total,
        "file": file_path
    }), flush=True)


def main():
    if len(sys.argv) < 3:
        sys.stderr.write("Usage: python -m src.mcp_server.index_pipeline <repo_root> <target_path>\n")
        sys.exit(1)

    repo_root = sys.argv[1]
    target_path = sys.argv[2]

    try:
        indexer = CodebaseIndexer(repo_path=repo_root)
        res = indexer.index_directory(target_path, progress_callback=progress_callback)
        print(json.dumps({"result": res}), flush=True)
    except Exception as e:
        sys.stderr.write(f"Error during indexing subprocess: {e}\n")
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
