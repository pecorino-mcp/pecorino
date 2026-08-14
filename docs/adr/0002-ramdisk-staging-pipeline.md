# 2. RAM-Disk Staging Pipeline for High-Throughput Ingestion

Date: 2026-08-14

## Status

Accepted

## Context

Indexing medium to large codebases produces tens of thousands of AST nodes, embeddings, and relationship edges. Direct random I/O writes to spinning disks or standard SSDs during iterative chunk processing create significant lock contention and write latency on DuckDB WAL and Kùzu storage.

## Decision

Implement a temporary in-memory staging mechanism using Linux `/dev/shm` (RAM-disk) via `RamdiskIndex`:
1. When indexing a repository, initialize DuckDB and Gorgonzola databases inside `/dev/shm`.
2. Process all AST parsing, embedding generation, graph edge insertion, and initial ranking in memory.
3. Upon indexing completion, flush database files atomically to persistent disk storage (`.pecorino/`).
4. Check available `/dev/shm` capacity before staging; fall back automatically to direct disk operations (`DummyContext`) if memory is insufficient.

## Consequences & Trade-offs

### Positive
- Near-instantaneous write throughput and zero physical disk I/O bottlenecks during batch extraction.
- Atomic commit: failing during extraction leaves the original disk database untouched until staging syncs.

### Brutal Realities & Flaws
- **OOM Vulnerability**: Running in constrained container environments (e.g. 6GB RAM or small Docker `/dev/shm` slices often defaulted to 64MB) causes fatal crashes if `required_ramdisk_bytes` sizing estimation fails or if `/dev/shm` fills up mid-index.
- **Resource Sizing Heuristics**: The projected database size heuristic (`total_source_bytes * 40.0 * 1.5`) is a rough approximation; large generated files, minified bundles, or large binary-like files can easily violate assumptions.
- **Process Abort Data Loss**: If the host process is killed with `SIGKILL` or power drops mid-indexing, uncommitted work in `/dev/shm` is lost immediately.
