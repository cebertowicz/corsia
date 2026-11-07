"""Movement reports omit Local only paths outside the report scope."""

import json

import pytest

from events.movement_report import deliver_movement_report_sync
from infra import config
from infra.conductor import set_project_conductor
from infra.corsia_agent import set_corsia_agent
from infra.database import get_db
from tests.registry_helpers import make_session


@pytest.mark.parametrize('breach', [False, True])
def test_movement_paths_filtered_before_delivery(isolated_db, monkeypatch, tmp_path, breach):
    monkeypatch.setattr(config, 'WORKSPACE', tmp_path)
    private_dir = tmp_path / 'projects' / 'private'
    private_dir.mkdir(parents=True)
    ordinary_dir = tmp_path / 'projects' / 'ordinary'
    ordinary_dir.mkdir(parents=True)
    db = get_db()
    try:
        db.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES ('private','private','t','t')")
        db.execute("INSERT INTO project_inference_policies(project_id,cloud_models_off) VALUES ('private',1)")
        db.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES ('ordinary','ordinary','t','t')")
        workspace = make_session(db, prefix='workspace')
        owner = make_session(db, project_id='private', prefix='owner')
        assert set_corsia_agent(workspace, db=db)
        assert set_project_conductor('private', owner, db=db)
        db.commit()
        secret = str(private_dir / 'secret.md')
        public = str(tmp_path / 'shared' / 'public.md')
        ordinary = str(ordinary_dir / 'skill.md')
        move = {'series_id': 's', 'owner_agent': owner, 'value': 3,
                'normal_range': [0, 1], 'preceding_changes': [
                    {'kind': 'skill', 'ref': secret, 'at': '2026-10-14T08:00:00+00:00'},
                    {'kind': 'skill', 'ref': public, 'at': '2026-10-14T08:00:00+00:00'},
                    {'kind': 'skill', 'ref': ordinary, 'at': '2026-10-14T08:00:00+00:00'},
                    {'kind': 'model', 'ref': 'provider/model', 'at': '2026-10-14T08:00:00+00:00'}]}
        for scope in ('workspace', 'private'):
            deliver_movement_report_sync({'scope': scope, 'report_date': '2026-10-15',
                                          'breach': breach, 'moves': [move]}, db=db)
        def delivered(thread):
            if breach:
                row = db.execute("SELECT content FROM messages WHERE thread_id=? AND id LIKE 'platform-event:%'", (thread,)).fetchone()
                return row['content']
            row = db.execute("SELECT summary, details_json FROM context_change_journal WHERE object_kind='thread_context' AND object_id=?", (thread,)).fetchone()
            return json.dumps(dict(row))
        workspace_text = delivered(workspace)
        owner_text = delivered(owner)
        assert secret not in workspace_text
        assert public in workspace_text
        assert ordinary in workspace_text
        assert 'provider/model' in workspace_text
        assert secret in owner_text
        assert public in owner_text
    finally:
        db.close()


def test_shared_repository_stays_visible_to_own_local_project(isolated_db, monkeypatch, tmp_path):
    from events.movement_report import MovementReport, _filter_local_only_paths

    monkeypatch.setattr(config, 'WORKSPACE', tmp_path)
    repo = tmp_path / 'repos' / 'shared'
    repo.mkdir(parents=True)
    db = get_db()
    try:
        for name in ('own', 'other'):
            source = tmp_path / 'projects' / name / 'source'
            source.mkdir(parents=True)
            (source / 'shared').symlink_to(repo, target_is_directory=True)
            db.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES (?,?,'t','t')", (name, name))
            db.execute("INSERT INTO project_inference_policies(project_id,cloud_models_off) VALUES (?,1)", (name,))
            db.execute("INSERT INTO project_repo_policies VALUES (?, 'shared', ?, NULL, 0)", (name, str(repo)))
        ref = str(repo / 'file.md')
        payload = {'scope': 'own', 'report_date': '2026-10-15', 'breach': False,
                   'moves': [{'series_id': 's', 'owner_agent': 'owner', 'value': 3, 'normal_range': [0, 1],
                              'preceding_changes': [{'kind': 'skill', 'ref': ref, 'at': '2026-10-14T08:00:00+00:00'}]}]}
        assert _filter_local_only_paths(MovementReport.parse(payload), db).moves[0].preceding_changes[0].ref == ref
        payload['scope'] = 'workspace'
        assert not _filter_local_only_paths(MovementReport.parse(payload), db).moves[0].preceding_changes
    finally:
        db.close()


def test_agent_scoped_filter_keeps_own_project(isolated_db, monkeypatch, tmp_path):
    from events.movement_report import MovementReport, _filter_local_only_paths

    monkeypatch.setattr(config, 'WORKSPACE', tmp_path)
    for name in ('own', 'other'):
        (tmp_path / 'projects' / name).mkdir(parents=True)
    db = get_db()
    try:
        for name in ('own', 'other'):
            db.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES (?,?,'t','t')", (name, name))
            db.execute("INSERT INTO project_inference_policies(project_id,cloud_models_off) VALUES (?,1)", (name,))
        owner = make_session(db, project_id='own', prefix='owner')
        refs = [str(tmp_path / 'projects' / name / 'file.md') for name in ('own', 'other')]
        report = MovementReport.parse({'scope': f'agent:{owner}', 'report_date': '2026-10-15', 'breach': False,
            'moves': [{'series_id': 's', 'owner_agent': owner, 'value': 3, 'normal_range': [0, 1],
                       'preceding_changes': [{'kind': 'skill', 'ref': ref, 'at': '2026-10-14T08:00:00+00:00'}
                                             for ref in refs]}]})
        result = _filter_local_only_paths(report, db)
        assert [change.ref for change in result.moves[0].preceding_changes] == refs[:1]
    finally:
        db.close()
