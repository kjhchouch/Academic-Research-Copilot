"""`python -m mcp_literature_search`.

Kept as a module entry point alongside the `literature-search` console script
because a stdio client config often needs a command that works without the venv's
Scripts directory being on PATH -- `python -m` only needs the interpreter.
"""

from mcp_literature_search.server import main

if __name__ == "__main__":
    main()
