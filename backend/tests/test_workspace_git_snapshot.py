"""Claude Code launch settings respect workspace Git history boundaries."""

import json
import subprocess

from infra import config
from infra.database import get_db
from subagents.cc_stream import CCStreamSession


def _settings(cwd):
    cmd = CCStreamSession(binary='claude', session_id='session', working_dir=str(cwd)).build_cmd()
    return json.loads(cmd[cmd.index('--settings') + 1])


def test_workspace_git_snapshot_off_registered_worktree_unchanged(isolated_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, 'WORKSPACE', tmp_path)
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    project = tmp_path / 'projects' / 'p'
    project.mkdir(parents=True)
    repo = project / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    worktree = repo / '.worktrees' / 'feature'
    worktree.mkdir(parents=True)
    gitdir = repo / '.git' / 'worktrees' / 'feature'
    gitdir.parent.mkdir(parents=True)
    subprocess.run(['git', 'init', '-q', '--separate-git-dir', str(gitdir), str(worktree)], check=True)
    (gitdir / 'gitdir').write_text(f'{worktree / ".git"}\n')
    db = get_db()
    try:
        db.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES ('p','p','t','t')")
        db.execute("INSERT INTO project_repo_policies VALUES ('p','repo',?,NULL,1)", (str(repo),))
        db.commit()
    finally:
        db.close()

    (project / '.git').mkdir()
    assert _settings(project)['includeGitInstructions'] is False
    assert 'includeGitInstructions' not in _settings(repo)
    assert 'includeGitInstructions' not in _settings(worktree)
    monkeypatch.chdir(project)
    inherited = CCStreamSession(binary='claude', session_id='session').build_cmd()
    assert json.loads(inherited[inherited.index('--settings') + 1])['includeGitInstructions'] is False
    confined = CCStreamSession(binary='claude', session_id='session', working_dir=str(project),
                               confinement='untrusted', mcp_config='{}').build_cmd()
    assert json.loads(confined[confined.index('--settings') + 1])['includeGitInstructions'] is False
