"""Platform selection for tests supplying Safehouse-specific shell mocks."""

from types import SimpleNamespace


def use_mac_shell(monkeypatch):
    import tools.controlled_git_sandbox as controlled_git_sandbox
    import tools.shell_envelope as shell_envelope
    import tools.shell_sandbox as shell_sandbox

    for module in (controlled_git_sandbox, shell_envelope, shell_sandbox):
        monkeypatch.setattr(module, "sys", SimpleNamespace(platform="darwin"))
