PROMPT = """Purpose:
Search UTF-8 text files line by line within a workspace directory. The default pattern mode is regular expression.

Use when:
- You need to find files or lines based on text content.
- You need nearby line context around content matches.

Prefer instead:
- Use `glob` when the selection depends only on path names or extensions.
- Use `read_file` when you already know the file and need its full contents.

Rules:
- The search root must be a directory.
- Searches are line-based. Narrow the search when a result is truncated.

Returns:
- Matching lines in stable path and line order, with line numbers.
"""
