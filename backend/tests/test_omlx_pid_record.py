"""The supervisor's spawn record can name the oMLX build it started, which is the only version proof CORSIA has."""

from __future__ import annotations

import os

from llm.providers import omlx_process as process


def test_a_stamped_record_names_the_build_and_an_unmanaged_one_records_nothing(tmp_path):
    process.write_pid(tmp_path, "seat", os.getpid(), omlx_version="0.7.3")

    assert process.recorded_omlx_version(tmp_path, "seat") == "0.7.3"

    process.write_pid(tmp_path, "unmanaged", os.getpid())  # a person's server records no build
    assert process.recorded_omlx_version(tmp_path, "unmanaged") is None
    assert process.recorded_omlx_version(tmp_path, "no-such-server") is None
