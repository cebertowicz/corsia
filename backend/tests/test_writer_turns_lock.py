"""Person holds change on the event loop while database workers read them and claim agent turns."""

from __future__ import annotations

import threading

from doceditor import writer_turns
from doceditor.writer_turns import PersonHold


def test_reads_and_claims_racing_page_moves_never_fail():
    writer_turns.reset()
    hold = PersonHold(user_id="u1", name="Person", artifact_id="a1")
    stop = threading.Event()
    errors: list[BaseException] = []

    def person():
        pages = [object() for _ in range(8)]
        while not stop.is_set():
            for page in pages:
                writer_turns.take(page, "root", hold)
            for page in pages:
                writer_turns.release(page)

    def agent():
        try:
            while not stop.is_set():
                writer_turns.people_on("root")
                writer_turns.person_holding("root")
                if writer_turns.agent_claim("root", "agent"):
                    writer_turns.agent_exit("root", "agent")
        except BaseException as error:  # noqa: BLE001 - the race under test raises RuntimeError
            errors.append(error)

    workers = [threading.Thread(target=person), *(threading.Thread(target=agent) for _ in range(3))]
    for worker in workers:
        worker.start()
    threading.Event().wait(0.5)
    stop.set()
    for worker in workers:
        worker.join()
    writer_turns.reset()

    assert errors == []
    assert writer_turns.agent_claim("root", "agent") is True


def test_a_claim_is_refused_while_a_person_or_another_agent_holds_the_thread():
    writer_turns.reset()
    page = object()
    writer_turns.take(page, "root", PersonHold(user_id="u1", name="Person", artifact_id="a1"))
    assert writer_turns.agent_claim("root", "b") is False
    writer_turns.release(page)
    assert writer_turns.agent_claim("root", "b") is True
    assert writer_turns.agent_claim("root", "b") is True  # the same agent's next write in its turn
    assert writer_turns.agent_claim("root", "c") is False
    writer_turns.reset()
