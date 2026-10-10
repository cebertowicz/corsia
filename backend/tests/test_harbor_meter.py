from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

# benchmarks/ sits at the repository root, outside backend/. The L3 job runs
# pytest from backend/, where the root is not on sys.path.
sys.path.append(str(Path(__file__).resolve().parents[2]))

from benchmarks.harbor.meter import (  # noqa: E402
    JsonlMeter,
    ProviderCallCapExceeded,
    harbor_totals,
    measured_call_cap,
    median_seconds_per_call,
)
from benchmarks.harbor.proxy import extract_usage  # noqa: E402


def test_meter_records_raw_usage_and_enforces_cap(tmp_path):
    path = tmp_path / "usage.jsonl"
    meter = JsonlMeter(path, unit_id="setup-corsia", call_cap=1)
    index = meter.reserve_call()
    usage = {"prompt_tokens": 4, "completion_tokens": 2, "prompt_tokens_details": {"cached_tokens": 1}}
    meter.record_response(index, {"usage": usage, "model": "m"}, time.monotonic() - 0.2)

    assert json.loads(path.read_text())["usage"] == usage
    assert harbor_totals(path) == {"input": 4, "cache": 1, "output": 2, "calls": 1, "calls_without_usage": 0}
    assert median_seconds_per_call(path) > 0
    with pytest.raises(ProviderCallCapExceeded):
        meter.reserve_call()


def test_calls_without_usage_stay_charged(tmp_path):
    path = tmp_path / "usage.jsonl"
    meter = JsonlMeter(path, unit_id="u", call_cap=5)
    meter.record_response(meter.reserve_call(), {}, time.monotonic())

    assert harbor_totals(path)["calls_without_usage"] == 1


def test_extract_usage_reads_chat_and_responses_streams():
    chat = b'data: {"model":"m","choices":[]}\n\ndata: {"model":"m","usage":{"prompt_tokens":3}}\n\ndata: [DONE]\n'
    responses = (
        b'event: response.completed\n'
        b'data: {"type":"response.completed","response":{"model":"r","usage":{"input_tokens":7}}}\n'
    )

    assert extract_usage(chat, "text/event-stream")["usage"] == {"prompt_tokens": 3}
    assert extract_usage(responses, "text/event-stream") == {"model": "r", "usage": {"input_tokens": 7}}
    assert extract_usage(b'{"usage":{"input_tokens":2}}', "application/json")["usage"] == {"input_tokens": 2}


def test_measured_cap_uses_contract_v2_formula():
    assert measured_call_cap(10) == 300
    assert measured_call_cap(1) == 1800
