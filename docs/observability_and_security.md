# Observability & Security

As Pecorino has evolved into an MCP-centric server processing complex codebases, observability and security have become paramount. 

## 1. Prometheus Telemetry & Metrics
Pecorino now features a robust Prometheus metrics setup. This telemetry framework is designed to monitor the health and performance of the multi-stage indexing pipeline, search latencies, and resource consumption.
- **Structured Logging**: Deeply integrated across extraction, chunking, and embedding phases.
- **Pipeline Monitoring**: Tracks the durations of individual stages (e.g., AST parsing, Gorgonzola graph updates, vector embedding generation) using the `IndexProfiler`.

## 2. Security Hardening & OAuth 2.1
To securely deploy the MCP Server as an SSE endpoint across networks, several security enhancements have been implemented:
- **OAuth 2.1 Authentication**: A comprehensive authentication flow secures the SSE transport layer, ensuring that only authorized AI agents and users can query the codebase or trigger indexing tasks.
- **Security Hardening**: General security improvements have been made to protect the AST index and graph database against unauthorized access.
