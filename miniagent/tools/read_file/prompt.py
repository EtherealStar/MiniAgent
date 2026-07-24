PROMPT = """Purpose:
Read a known UTF-8 text file by line range and show stable line numbers.

Use when:
- You know the exact file path and need its contents.
- You need to page through a committed tool result or converted document.

Prefer instead:
- Use `glob` when you need to discover a path.
- Use `grep` when you need to search file contents.
- Use `read_docs` to convert a PDF or Word document before reading it.

Rules:
- `offset` is the number of lines to skip and `limit` is the maximum number of lines to return.
- Use the returned `next_offset` to continue reading.
- Reduce the line range if the result exceeds the inline byte or token budget.

Returns:
- UTF-8 text with 1-based file line numbers, page metadata, and the full-file SHA-256.
- The result states whether more lines remain and is never externalized.

If it fails:
- Choose a supported text file, correct the path, or request a smaller line range.
"""
