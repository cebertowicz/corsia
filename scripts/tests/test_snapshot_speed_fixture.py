"""The read-cost fixture shares a real source version across comment roots."""
import hashlib
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from speed_gate import fixture_build  # noqa: E402


def test_snapshot_fixture_links_roots_and_leaves_replies_unversioned():
    db = sqlite3.connect(':memory:')
    db.executescript('''
        CREATE TABLE doc_comment_snapshots (id INTEGER PRIMARY KEY, digest BLOB NOT NULL, text BLOB NOT NULL);
        CREATE TABLE doc_comments (id TEXT, parent_id TEXT, snapshot_id INTEGER, anchor_text TEXT,
                                   from_off INTEGER, to_off INTEGER, anchor_prefix TEXT, anchor_suffix TEXT);
        INSERT INTO doc_comments (id, parent_id) VALUES ('a', NULL), ('b', NULL), ('reply', 'a');
    ''')
    manifest = {'tables': {}}
    fixture_build._seed_comment_snapshots(db, manifest)
    snapshot_id, digest, text = db.execute('SELECT id, digest, text FROM doc_comment_snapshots').fetchone()
    assert digest == hashlib.sha256(text).digest()
    roots = db.execute('SELECT snapshot_id, anchor_text, from_off, to_off FROM doc_comments WHERE parent_id IS NULL').fetchall()
    assert roots == [(snapshot_id, 'Fixture anchor', 0, 14)] * 2
    assert text.decode('utf-8')[0:14] == 'Fixture anchor'
    assert db.execute("SELECT snapshot_id FROM doc_comments WHERE id='reply'").fetchone()[0] is None
    assert manifest['tables']['doc_comment_snapshots'] == {'target': 1, 'rows': 1}


def test_snapshot_fixture_accepts_a_base_without_the_table():
    fixture_build._seed_comment_snapshots(sqlite3.connect(':memory:'), {'tables': {}})
