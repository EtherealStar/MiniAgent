PROMPT = """Purpose:
Discover files and directories by path pattern within a workspace directory.

Use when:
- You need to find paths by name, extension, or directory structure.

Prefer instead:
- Use `grep` when the selection depends on text content.

Rules:
- Patterns match complete paths relative to the search root.
- Use `**` as a complete segment to cross directory levels.

Returns:
- Workspace-relative paths in stable order. Directories end with `/`.
"""
