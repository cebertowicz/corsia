"""Measure document_rows with synthetic unread replies.

Usage: benchmark_activity_rows.py <unread-replies> [documents]

With one document (default) the fixture has one registry-titled document, so no row pays
the bounded title head. With more documents the replies spread across that many documents
whose registry titles are their file names, so every row resolves its own first heading:
the first call pays one 16 KiB head read per document (cold), later polls pay a stat per
document and re-read nothing (warm, cache hit).
"""
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

from infra.schema import SCHEMA_SQL
from doceditor.activity_reply_rows import document_rows
from api.attention import activity_lists

count = int(sys.argv[1])
docs = int(sys.argv[2]) if len(sys.argv) > 2 else 1
db = sqlite3.connect(':memory:')
db.row_factory = sqlite3.Row
db.executescript(SCHEMA_SQL)
db.execute("INSERT INTO threads(id,user_id,title,created_at,updated_at) VALUES ('t','u','Agent','0','0')")
title_dir = Path(tempfile.mkdtemp(prefix='activity-title-bench-'))
for i in range(docs):
    doc_id = 'd' if docs == 1 else f'd{i}'
    name = 'Document' if docs == 1 else f'doc{i}.md'
    path = None if docs == 1 else str(title_dir / f'doc{i}.md')
    if path:
        Path(path).write_text(f'# Heading {i}\n\nSynthetic body.\n')
    db.execute("INSERT INTO artifact_identities(id,integration,type,external_id,title,path,created_at,updated_at) VALUES (?,'local','document',?,?,?,'0','0')", (doc_id, doc_id, name, path))
    db.execute("INSERT INTO artifact_registrations(id,artifact_id,parent_thread_id,created_at,updated_at) VALUES (?,?,'t','0','0')", (f'r{doc_id}', doc_id))
    db.execute("INSERT INTO doc_comments(id,artifact_id,author,author_kind,body,created_at,updated_at) VALUES (?,?,'person','user','q','0','0')", (f'root{doc_id}', doc_id))
db.executemany("INSERT INTO doc_comments(id,artifact_id,parent_id,author,author_kind,author_thread_id,body,created_at,updated_at) VALUES (?,?,?,'agent','agent','t','a',?,?)",
               ((f'c{i:06}', 'd' if docs == 1 else f'd{i % docs}', 'rootd' if docs == 1 else f'rootd{i % docs}', f'{i:06}', f'{i:06}') for i in range(count)))
db.commit()
statements = []
db.set_trace_callback(statements.append)
start = time.perf_counter()
result = document_rows(db, 'u')
cold_ms = (time.perf_counter() - start) * 1000
db.set_trace_callback(None)
assert sum(r['detail']['unread'] for r in result) == count and len(result) == docs
route_statements = []
db.set_trace_callback(route_statements.append)
activity_lists(db, 'u', view='activity')
db.set_trace_callback(None)
print(f'activity_lists statements={len(route_statements)}')
samples = []
for _ in range(21):
    start = time.perf_counter()
    document_rows(db, 'u')
    samples.append((time.perf_counter() - start) * 1000)
samples.sort()
warm = f'p50_ms={statistics.median(samples):.1f} p95_ms={samples[19]:.1f}'
if docs == 1:
    print(f'unread={count} statements={len(statements)} samples={len(samples)} {warm}')
else:
    print(f'unread={count} docs={docs} statements={len(statements)} cold_ms={cold_ms:.1f} warm {warm}')
