PROMPT = """Purpose:
Convert a PDF or Word document to a session-scoped Markdown document with MinerU.

Use when:
- You need to read a known PDF, DOC, or DOCX file.

Prefer instead:
- Use read_file for UTF-8 text or for the Markdown DocumentRef returned by this tool.
- Use a spreadsheet or image tool for other document types.

Rules:
- The source document is uploaded to MinerU and requires an external data-transfer permission decision.
- After conversion, call read_file on the returned controlled Markdown path to page through the contents.

Returns:
- A completed DocumentRef, source type, cache status, and Markdown integrity metadata; it does not return the document text.

If it fails:
- Choose a supported document, allow the external upload, fix MinerU configuration, or call the tool again after a timeout.
"""
