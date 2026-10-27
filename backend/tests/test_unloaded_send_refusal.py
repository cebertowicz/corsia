"""Local readiness refuses a new human send without accepting a row."""

import pytest


@pytest.mark.parametrize("cause", ["no_room", "no_memory", "unreachable"])
def test_human_send_refused_before_message_write(isolated_db, monkeypatch, cause):
    from infra.database import get_db
    from infra.input_admission import accept_thread_input
    from infra.send_types import SendRejected
    from llm.local_model_load import Readiness
    from tests.unavailable_model_support import seed_model

    seed_model("omlx@srv/m")
    db = get_db()
    try:
        db.execute("INSERT INTO threads (id,user_id,model,created_at,updated_at) VALUES ('t','u','omlx@srv/m','t0','t0')")
        db.commit()
        monkeypatch.setattr("llm.local_model_load.readiness", lambda *_a, **_k: Readiness(cause, 2))
        with pytest.raises(SendRejected) as raised:
            accept_thread_input("t", role="user", content="hi", intent="apply_now", message_id="m", db=db)
        assert (raised.value.status_code, raised.value.detail) == (409, "Couldn't load the selected local model.")
        assert raised.value.metadata == {"cause": cause, "other_loaded": 2}
        assert db.execute("SELECT count(*) FROM messages WHERE thread_id='t'").fetchone()[0] == 0
    finally:
        db.close()
