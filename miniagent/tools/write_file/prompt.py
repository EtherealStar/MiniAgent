PROMPT = """Purpose:
Create a UTF-8 text file or atomically replace a previously read version.

Use when:
- You need to write the complete contents of one known text file.

Rules:
- Omitting `expected_sha256` is create-only and fails if the file already exists.
- To replace a file, read it first and pass the full-file SHA-256 returned by `read_file`.
- Supply the complete desired content; this tool does not merge with existing text.

Returns:
- Whether the file was created or replaced, its byte count, and its new SHA-256.
"""
