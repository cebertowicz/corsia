"""Commands for regenerating documentation with the project virtual environment."""


def regenerate_command(generator: str) -> str:
    return f".venv/bin/python scripts/docs/{generator}.py"
