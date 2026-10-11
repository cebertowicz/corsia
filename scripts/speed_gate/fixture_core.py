"""The relational core of the fixture: projects, threads, messages, usage and plans.

These tables carry the skew a per-parent read is sensitive to, so they are
built by hand to match the census (messages and usage per thread, nodes per
plan) and include exactly one thread, and one plan, at the production maximum.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from datetime import timedelta

from .sqlite_pin import sqlite3
from . import fixture_spec as spec
from .fixture_rows import END, START, Pools, timestamp

THREAD_COUNT = round(spec.PRODUCTION_ROWS["threads"] * spec.SCALE)
PROJECT_COUNT = spec.SMALL_ROWS["projects"]
AGENT_THREADS = 18
MODELS = ("claude-code/opus", "claude-code/sonnet", "codex/gpt-6.1-sol", "codex/gpt-5.6-terra",
          "external@fixture/qwen3.8-flash", "omlx@local/fixture-local")
ROLES = ("user", "assistant", "assistant", "tool", "assistant", "system")


def _blocks(rng: random.Random) -> list[str]:
    """Reusable message bodies of mixed size (most short, a tail of long tool output)."""
    words = [f"w{n:03d}" for n in range(400)]
    sizes = [40] * 14 + [180] * 5 + [900] * 1
    return [" ".join(rng.choices(words, k=rng.choice(sizes))) for _ in range(2_048)]


def _parent_counts(knots, parents: int, rng: random.Random, maximum: int) -> list[int]:
    """Per-parent counts in a deterministic shuffled order; parent 0 is the maximum."""
    counts = spec.quantile_rows(knots, parents - 1)
    rng.shuffle(counts)
    return [maximum, *counts]


def seed_counts(rng: random.Random) -> tuple[list[int], list[int]]:
    """(messages per thread, usage per thread), thread 0 at the production maximum."""
    with_messages = round(5_367 * spec.SCALE)
    with_usage = round(4_806 * spec.SCALE)
    return (_parent_counts(spec.MESSAGE_KNOTS, with_messages, rng, spec.MAX_THREAD_MESSAGES),
            _parent_counts(spec.USAGE_KNOTS, with_usage, rng, spec.MAX_THREAD_USAGE))


def seed_projects(db: sqlite3.Connection, rng: random.Random, pools: Pools) -> None:
    rows = []
    for index in range(PROJECT_COUNT):
        stamp = timestamp(rng)
        rows.append((f"project-{index:02d}", f"Fixture project {index:02d}", f"/fixture/p{index:02d}",
                     "active" if index < 17 else "archived", stamp, stamp))
    db.executemany("INSERT INTO projects (id,name,path,status,created_at,updated_at)"
                   " VALUES (?,?,?,?,?,?)", rows)
    pools.add("project", [r[0] for r in rows])


def _thread_rows(rng: random.Random, message_counts: list[int]) -> Iterator[tuple]:
    """Thread 0 is the conductor's: the largest thread, in the largest project."""
    horizon = int((END - START).total_seconds() * 0.9)
    for index in range(THREAD_COUNT):
        created = START + timedelta(seconds=rng.randrange(horizon))
        if index == 0 or rng.random() < 0.45:
            project = "project-00"
        else:
            project = None if rng.random() < 0.1 else f"project-{rng.randrange(PROJECT_COUNT):02d}"
        state = "active" if rng.random() < 0.85 else ("dormant" if rng.random() < 0.7 else "archived")
        count = message_counts[index] if index < len(message_counts) else 0
        yield (f"thread-{index:05d}", "steve", f"Fixture thread {index}", MODELS[index % len(MODELS)],
               created.isoformat(), END.isoformat(), project, state,
               1 if index < AGENT_THREADS else 0, count + 1)


def seed_threads(db: sqlite3.Connection, rng: random.Random, pools: Pools,
                 message_counts: list[int]) -> None:
    rows = list(_thread_rows(rng, message_counts))
    db.executemany(
        "INSERT INTO threads (id,user_id,title,model,created_at,updated_at,project_id,"
        "lifecycle_state,agent_enabled,next_message_seq) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    db.execute("UPDATE threads SET agent_status='active', agent_responsibility='fixture owner'"
               " WHERE agent_enabled=1")
    # A third of the non-agent threads are workers spawned by one of the agents.
    db.execute("UPDATE threads SET spawned_by = ('thread-' || printf('%05d', rowid % ?)),"
               " parent_thread_id = ('thread-' || printf('%05d', rowid % ?))"
               " WHERE agent_enabled=0 AND rowid % 3 = 0", (AGENT_THREADS, AGENT_THREADS))
    ids = [r[0] for r in rows]
    weights = [float(message_counts[i]) ** 0.5 if i < len(message_counts) else 1.0
               for i in range(len(ids))]
    pools.add("thread", ids, weights)


def _message_rows(rng: random.Random, counts: list[int], blocks: list[str]) -> Iterator[tuple]:
    lead = int((END - START).total_seconds() * 0.1)
    for index, count in enumerate(counts):
        begin = START + timedelta(seconds=rng.randrange(lead))
        step = (END - begin).total_seconds() / (count + 1)
        user_order = 0
        for k in range(count):
            role = ROLES[(k + index) % len(ROLES)] if k else "user"
            order = None
            if role == "user":
                user_order += 1
                order = user_order
            yield (f"msg-{index:05d}-{k:06d}", f"thread-{index:05d}", role,
                   f"{blocks[rng.randrange(len(blocks))]} #{k}",
                   (begin + timedelta(seconds=step * (k + 1))).isoformat(), k + 1, order)


def seed_messages(db: sqlite3.Connection, rng: random.Random, counts: list[int]) -> int:
    blocks = _blocks(rng)
    cursor = db.executemany(
        "INSERT INTO messages (id,thread_id,role,content,created_at,seq,input_order)"
        " VALUES (?,?,?,?,?,?,?)", _message_rows(rng, counts, blocks))
    return cursor.rowcount


def seed_usage(db: sqlite3.Connection, rng: random.Random, counts: list[int]) -> int:
    accounts = [r[0] for r in db.execute("SELECT id FROM vendor_accounts ORDER BY id")] or [None]

    def rows() -> Iterator[tuple]:
        for index, count in enumerate(counts):
            for k in range(count):
                prompt, completion = rng.randrange(500, 90_000), rng.randrange(20, 4_000)
                yield (f"usage-{index:05d}-{k:06d}", f"thread-{index:05d}", f"turn-{index:05d}-{k:06d}",
                       accounts[k % len(accounts)], MODELS[(index + k) % len(MODELS)], prompt,
                       completion, prompt // 3, prompt // 2, prompt + completion,
                       round((prompt * 3 + completion * 15) / 1_000_000, 6), timestamp(rng))

    return db.executemany(
        "INSERT INTO llm_usage (id,thread_id,turn_id,vendor_account_id,model,prompt_tokens,"
        "completion_tokens,cache_creation_tokens,cache_read_tokens,total_tokens,cost_usd,created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows()).rowcount
