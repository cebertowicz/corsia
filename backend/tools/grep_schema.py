"""Agent-facing description and parameters for grep."""

DESCRIPTION = (
    "Search file contents using regex patterns. Built on ripgrep for speed.\n\n"
    "Supports full regex syntax (e.g., 'log.*Error', 'function\\s+\\w+', 'TODO|FIXME').\n"
    "Filter files with the glob parameter (e.g., '*.py', '**/*.ts').\n"
    "Skips hidden files and folders (except your own thread's .threads folder), "
    "node_modules, __pycache__ and venv unless the glob names them (for example "
    "'.github/**/*.yml'); passing one as path also searches inside it.\n"
    "Output is capped at ~20K characters. Use 'files' mode for broad searches, "
    "'content' mode with glob filters for targeted results.\n\n"
    "Output modes:\n"
    "- 'content': shows matching lines with file path and line number\n"
    "- 'files': shows only file paths that contain matches (default)\n"
    "- 'count': shows match count per file\n\n"
    "USE THIS when you need to:\n"
    "- Find where a function, class, or variable is defined or used\n"
    "- Search for specific strings or patterns across the codebase\n"
    "- Find TODO/FIXME comments, error messages, or log patterns\n"
    "- Locate imports or references to a module\n\n"
    "DO NOT use this when:\n"
    "- Finding files by name (use glob instead)\n"
    "- Reading a specific file (use read_file instead)\n"
    "- Listing directory contents (use list_dir instead)"
)

PARAMETERS = {
    "type": "object",
    "properties": {
        "pattern": {"type": "string", "description": "Regex pattern to search for in file contents"},
        "path": {"type": "string", "description": "File or directory to search in. Defaults to workspace root."},
        "glob": {"type": "string", "description": "Glob pattern to filter files (e.g., '*.py', '*.{ts,tsx}')"},
        "output_mode": {
            "type": "string", "enum": ["content", "files", "count"],
            "description": "Output mode: 'content' (matching lines), 'files' (file paths only, default), 'count' (match counts)",
        },
        "case_insensitive": {"type": "boolean", "description": "Case insensitive search. Default: false."},
        "context": {
            "type": "integer", "minimum": 0, "maximum": 10,
            "description": "Number of context lines before and after each match. Only for 'content' mode.",
        },
        "max_results": {
            "type": "integer", "minimum": 1, "maximum": 200,
            "description": "Maximum number of results to return. Default: 50.",
        },
        "all_threads": {
            "type": "boolean",
            "description": "Also search other threads' .threads folders. Default: false — search covers shared files and your own thread's folder.",
        },
    },
    "required": ["pattern"],
}
