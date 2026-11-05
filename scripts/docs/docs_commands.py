"""Commands for regenerating documentation with the backend environment."""


def regenerate_command(generator: str) -> str:
    return f"python scripts/docs/{generator}.py"
