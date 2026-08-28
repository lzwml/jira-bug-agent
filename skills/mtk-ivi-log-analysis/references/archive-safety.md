# Archive Handling Boundary

Use this reference when implementing or reviewing an archive extraction tool. The analysis Skill itself must not perform filesystem extraction.

A controlled extractor must:

- write only beneath a server-approved Case directory or isolated temporary directory;
- reject absolute paths, drive-qualified paths, `..` traversal, symlinks and unsafe junctions;
- enforce compressed size, expanded byte, file-count, nesting-depth and runtime budgets;
- avoid executing archive contents;
- preserve archive and member provenance;
- detect partial extraction and report it explicitly;
- support ZIP, TAR/TAR.GZ and plain GZ with format-appropriate libraries or tools.

PowerShell `Expand-Archive` is a ZIP operation and is not the generic handler for `.gz` or `.tar.gz`. Prefer a deterministic MCP/Core implementation so that every Agent run shares the same validation and budgets.

If controlled extraction is unavailable, register the archive as an opaque artifact and return missing evidence for claims that depend on its contents.

In this repository, the default controlled path is `inspect_archive` →
`extract_archive_members` → `build_index`. Inspection returns stable member IDs without
writing member content; extraction writes only selected members beside their source archive,
registers `archive!/member` provenance, and does not recursively expand nested archives.
Inspect a selected nested archive in a separate step. `prepare_case` remains the explicit
full-extraction fallback. An unsupported or rejected archive remains opaque; do not bypass
the result with ad-hoc shell extraction.
