# Security notes

- The MCP server and demo webapp expose **read-only** operators; no write
  operator is exposed to planners (Phase 1–2). The webapp binds 127.0.0.1
  by design — access it remotely via SSH port forwarding, and do not expose
  it directly to untrusted networks (it has no authentication).
- Prompt-injection defense: all stored-data strings enter LLM prompts
  escaped and length-capped inside `<data>` fences with a fixed
  data-is-never-instructions policy (see spec v1.1 WP2.1 and the red-team
  fixtures in `tests/test_spec_v11.py`).
- To report a vulnerability, open a private security advisory on GitHub or
  contact the maintainer directly.
