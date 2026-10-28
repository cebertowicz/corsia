"""Person-step cards follow the plan's creation and settlement state."""

from infra import thread_updates
from plans import archival, followups, mutations, update_cards
from tests.test_plan_archival import _plan, _task, db  # noqa: F401
from tests.test_plan_update_cards import _cards, _close, _review_then_accept


def _step_card(db, node_id):
    cards = [c for c in _cards(db) if c['kind'] == 'person_step' and c['node_id'] == node_id]
    assert len(cards) == 1
    return cards[0]


def test_person_step_does_not_change_sidebar_update_or_preview(db):
    plan = _plan(db)
    other = _task(db, plan.id, title='Previous task')
    _close(db, plan.id, other.id)
    before = thread_updates.latest_by_thread_with_attention(db, ['root'])
    assert before[0]['root']['summary'] == 'Closed: Previous task'
    _task(db, plan.id, title='Person task', assignee='user')
    assert thread_updates.latest_by_thread_with_attention(db, ['root']) == before


def test_creation_and_follow_up_emit_once_and_agent_task_does_not(db):
    plan = _plan(db)
    person = _task(db, plan.id, title='Person task', assignee='user')
    agent = _task(db, plan.id, title='Agent task')
    follow = followups.add_follow_up(person.id, title='Follow up', acceptance_criteria='Done', size='S', db=db)
    assert _step_card(db, person.id)['step_state'] == 'open'
    assert _step_card(db, follow.id)['step_state'] == 'open'
    assert not [c for c in _cards(db) if c['kind'] == 'person_step' and c['node_id'] == agent.id]


def test_split_emits_only_one_new_card(db):
    plan = _plan(db)
    source = _task(db, plan.id, title='Original', assignee='user')
    before = [c for c in _cards(db) if c['kind'] == 'person_step']
    sibling = mutations.split_node(
        source.id, title='Sibling', acceptance_criteria='Done', size='S',
        description='Complete the step. Already tried: the supported route.', db=db,
    )
    after = [c for c in _cards(db) if c['kind'] == 'person_step']
    assert len(after) == len(before) + 1
    assert sum(c['node_id'] == source.id for c in after) == 1
    assert sum(c['node_id'] == sibling.id for c in after) == 1


def test_reedit_and_event_replay_do_not_duplicate_the_card(db):
    plan = _plan(db)
    node = _task(db, plan.id, title='Person task', assignee='user')
    event = db.execute(
        "SELECT id, created_at FROM plan_node_events WHERE node_id=? AND event='node_created'", (node.id,),
    ).fetchone()
    mutations.edit_node(node.id, title='Renamed task', db=db)
    update_cards.observe_node_event(
        db, node_id=node.id, event='node_created', event_id=event['id'],
        new_state=None, payload=None, created_at=event['created_at'],
    )
    assert _step_card(db, node.id)['title'] == 'Person task'


def test_closed_and_archived_steps_never_wait(db):
    plan = _plan(db)
    closed = _task(db, plan.id, title='Closed task', assignee='user')
    archived = _task(db, plan.id, title='Archived task', assignee='user')
    _close(db, plan.id, closed.id)
    archival.archive_node(archived.id, db=db)
    assert _step_card(db, closed.id)['step_state'] == 'closed'
    assert _step_card(db, archived.id)['step_state'] == 'closed'


def test_accepted_step_reads_done_even_after_archive(db):
    plan = _plan(db)
    node = _task(db, plan.id, title='Done task', assignee='user')
    _review_then_accept(db, node.id)
    assert _step_card(db, node.id)['step_state'] == 'done'
    archival.archive_node(node.id, db=db)
    assert _step_card(db, node.id)['step_state'] == 'done'
