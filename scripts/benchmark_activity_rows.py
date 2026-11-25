"""Measure document_rows with synthetic unread replies in one document."""
import sqlite3
import statistics
import sys
import time

from infra.schema import SCHEMA_SQL
from doceditor.activity_reply_rows import document_rows
from api.attention import activity_lists

count = int(sys.argv[1])
db = sqlite3.connect(':memory:')
db.row_factory = sqlite3.Row
db.executescript(SCHEMA_SQL)
db.execute("INSERT INTO threads(id,user_id,title,created_at,updated_at) VALUES ('t','u','Agent','0','0')")
db.execute("INSERT INTO artifact_identities(id,integration,type,external_id,title,created_at,updated_at) VALUES ('d','local','document','d','Document','0','0')")
db.execute("INSERT INTO artifact_registrations(id,artifact_id,parent_thread_id,created_at,updated_at) VALUES ('r','d','t','0','0')")
db.execute("INSERT INTO doc_comments(id,artifact_id,author,author_kind,body,created_at,updated_at) VALUES ('root','d','person','user','q','0','0')")
db.executemany("INSERT INTO doc_comments(id,artifact_id,parent_id,author,author_kind,author_thread_id,body,created_at,updated_at) VALUES (?,'d','root','agent','agent','t','a',?,?)", ((f'c{i:06}', f'{i:06}', f'{i:06}') for i in range(count)))
db.commit()
statements = []
db.set_trace_callback(statements.append)
result = document_rows(db, 'u')
db.set_trace_callback(None)
assert result[0]['detail']['unread'] == count
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
print(f'unread={count} statements={len(statements)} samples={len(samples)} p50_ms={statistics.median(samples):.1f} p95_ms={samples[19]:.1f}')
