PROMPT = """Purpose:
Search the public web and return concise source results from Tavily.

Use when:
- You need current or externally published information that is not available in the workspace or conversation.

Prefer instead:
- Use workspace tools when the answer depends on local files.
- Use a dedicated web page reader when you need the full contents of a known URL.

Rules:
- Write a focused search query. Refine the query in a new call if the results are insufficient.
- Treat snippets as search summaries and use the returned URLs as sources; this tool does not read full pages.

Returns:
- Up to five ranked results with title, HTTP(S) URL, snippet, and an optional published date.
- No result is a successful search outcome, not an execution failure.

If it fails:
- Retry with a revised query only for a valid search that returned insufficient results; configuration, authentication, quota, or service failures require their indicated recovery.
"""
