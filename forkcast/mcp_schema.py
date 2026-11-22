"""Local MCP host grants, receipt digests and rolling call counts."""

MCP_SCHEMA = """
CREATE TABLE IF NOT EXISTS mcp_hosts (
    token_digest TEXT PRIMARY KEY NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    principal_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('STUDENT','OPS','EXTERNAL_AGENT')),
    venues_json TEXT NOT NULL,
    tools_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mcp_interactions (
    token_digest TEXT PRIMARY KEY NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    principal_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    argument_digest TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS mcp_calls (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    principal_id TEXT NOT NULL,
    happened_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS mcp_calls_by_subject ON mcp_calls(run_id, principal_id, happened_at);
"""
