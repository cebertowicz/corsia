"""Build a controlled Git envelope without a Linux profile file."""
from __future__ import annotations
import os
from pathlib import Path
import shlex
import sys
from tools.private_runtime_files import SHELL_ENVELOPE_DIR, private_mkstemp


def envelope(repo, args, *, policy, operation, policies, agent_account, env,
             thread_id, build_profile, build_argv):
    if sys.platform == 'linux':
        from tools.linux_shell import shell_argv
        command = shlex.join(['git', '-c', 'core.hooksPath=/dev/null', *args])
        return None, shell_argv(command, repo, thread_id=thread_id, policies=policies,
                                env=env, agent_account=agent_account,
                                controlled_policy=policy, controlled_operation=operation)
    descriptor, raw_path = private_mkstemp(SHELL_ENVELOPE_DIR, prefix='corsia-controlled-git-', suffix='.sb')
    profile_path = Path(raw_path).resolve(strict=False)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            handle.write(build_profile('', repo, profile_path, leased=False,
                controlled_policy=policy, controlled_operation=operation,
                policies=policies, agent_account=agent_account))
        argv = build_argv('', repo, profile_path, agent_account=agent_account)
        return profile_path, [*argv[:argv.index('--')], '--', 'git', '-c',
                              'core.hooksPath=/dev/null', *args]
    except BaseException:
        profile_path.unlink(missing_ok=True)
        raise
