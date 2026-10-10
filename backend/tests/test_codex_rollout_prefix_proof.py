from __future__ import annotations

import hashlib
import json


def _line(record: dict[str, object]) -> bytes:
    return json.dumps(record).encode() + b"\n"


def test_prefix_proof_streams_frozen_prefix_without_reading_it_whole(
    tmp_path,
    monkeypatch,
) -> None:
    from llm import codex_rollout_files as files

    prefix = b"".join(
        (
            _line(
                {
                    "type": "session_meta",
                    "payload": {
                        "id": "123e4567-e89b-12d3-a456-426614174000",
                        "cwd": "/tmp/project",
                    },
                }
            ),
            _line(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 11,
                                "cached_input_tokens": 7,
                                "output_tokens": 3,
                            },
                        },
                    },
                }
            ),
        )
    )
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_bytes(prefix + b"x" * 1024 * 1024)

    def whole_prefix_read(*_args, **_kwargs):
        raise AssertionError("prefix proof must not allocate the frozen prefix")

    monkeypatch.setattr(files, "read_codex_rollout_prefix", whole_prefix_read)

    size, digest, metadata, restored = files.codex_rollout_prefix_proof(
        rollout,
        len(prefix),
    )

    assert size == len(prefix)
    assert digest == hashlib.sha256(prefix).hexdigest()
    assert metadata["cwd"] == "/tmp/project"
    assert restored == {
        "input_tokens": 11,
        "cached_input_tokens": 7,
        "output_tokens": 3,
    }
