"""Words a thread reads when a plan result it did not cause reaches it.

Text constants and text formatting functions only (package rule). Every plan
wake line, its transcript chip, and the pause lines live here;
``prompts.plan_tools`` keeps the words of a tool's own reply and ``next`` block.
"""

from __future__ import annotations

from typing import Literal


#: What a thread is told when a plan result it did not cause reaches it. Each
#: line is followed by the same `next` block a close returns.
WAKE_TASK_CLOSED = "[CORSIA plan] {title} was closed by another actor."
WAKE_WORK_SELECTED = "[CORSIA plan] {title} is now your current task."
WAKE_ASK_RELEASED = "[CORSIA plan] {summary}"
#: The ask-answer sentence the wake and its plan receipt share. A verdict or
#: note the user did not give is left out rather than named as missing.
ASK_ANSWERED = 'The user {done} "{ask}"'
ASK_UNAVAILABLE = 'Your ask "{ask}" became unavailable'
ASK_RELEASE_SUMMARY = '{event} for {kind} "{title}" (node {node_id}).{verdict}{note} Other holds remain: {other}.'
ASK_RELEASE_VERDICT = " Verdict: {verdict}."
ASK_RELEASE_NOTE = " User answer: {note}."
WAKE_CHILD_RETURNED = (
    "[CORSIA plan] The work delegated for {title} has returned and awaits your verdict."
)
WAKE_CHILD_NEEDS_INPUT = (
    "[CORSIA plan] The worker on {title} is blocked on you: {reason} Answer with "
    "delegate_send(thread_id={worker}, plan_node_id={step})."
)
WAKE_REVISION_REQUESTED = (
    "[CORSIA plan] Your returned work on {title} was rejected; the revision is now yours."
)
#: The revision_requested text for a delegated worker, which works a step seeded in its own plan.
REVISION_WORKER_STEP = (
    "[CORSIA plan] Your returned work on {title} was rejected. Work step {worker_step_id} "
    "and return it with request_review; revision {revision_id} is the parent's reference."
)
#: A task in another thread's plan that one of yours depends on settled (#1552).
WAKE_PREREQUISITE_SETTLED = "[CORSIA plan] {prerequisite} in {owner}'s plan {outcome}"
#: A conductor-added task reached a terminal outcome in the receiving agent's plan.
WAKE_CONDUCTOR_TASK_SETTLED = (
    "[CORSIA plan] The task you added, {title}, in {receiver}'s plan {outcome}: {detail}."
    " Check that the outcome is what you needed; add a new task if it is not."
)
#: How a conductor-added task settled, keyed by its terminal state or archive.
CONDUCTOR_TASK_OUTCOMES = {
    "accepted": "is done",
    "accepted_with_followup": "is done, with a follow-up",
    "failed": "failed",
    "closed": "was closed",
    "archived": "was archived",
}
#: What each way of settling means for the dependent, keyed by outcome.
PREREQUISITE_OUTCOMES = {
    "done": "is done; {dependent} can go.",
    "failed": "failed; {dependent} cannot go until you remove that dependency or close it.",
    "removed": (
        "was {how} before it was done; {dependent} no longer waits on it."
        " Check whether it still needs that work."
    ),
}
#: A task in your own plan that one of yours depends on failed or was closed by
#: someone else: what ended, how, and the three ways on. Keyed by outcome.
PREREQUISITE_OWN_PLAN_LINES = {
    "failed": (
        "[CORSIA plan] {prerequisite} failed, and {dependent} depends on it."
        " {dependent} cannot go until you remove that dependency, depend on its"
        " replacement instead, or close it."
    ),
    "closed": (
        "[CORSIA plan] {prerequisite} was closed: {reason}. {dependent} depended"
        " on it; that dependency is gone, so {dependent} is ready. Go ahead with it,"
        " depend on its replacement instead, or close it."
    ),
}
#: A close whose reason cannot be read, said plainly rather than left blank.
CLOSE_REASON_MISSING = "no reason recorded"
#: A delegated worker stopped without leaving work behind. Both name what
#: happened, what it means for the parent, and the one thing to do about it.
WAKE_CHILD_RETURNED_NOTHING = (
    "[CORSIA plan] The child delegated for {title} ended its turn with an empty"
    " plan: it returned nothing, so the work is not underway. Re-brief the child"
    " with what it is to do, or stop it."
)
#: A returned worker the parent sent back by message has finished that round
#: (D4); the harvest reports each attachment once, so this is its return.
WAKE_CHILD_ROUND_RETURNED = (
    "[CORSIA plan] The child delegated for {title} worked again after it"
    " returned and has now stopped with nothing left open, so it is waiting"
    " on you. Its step still awaits your verdict: read its reply with"
    " delegate_status, then accept, reject with feedback, or fail the step."
)
#: A step failed during the worker's Turn and the worker stopped with other
#: work still open, so no return is coming yet (decided at Turn end, #2216).
WAKE_CHILD_STEP_FAILED = (
    "[CORSIA plan] A step of the work delegated for {title} failed, and the"
    " child stopped with other work still open, so no return is coming yet."
    " Decide whether to retry it, re-scope it, stop the child, or fail the"
    " delegated step. If the child only replaced that step, nothing failed:"
    " have the child archive the failed step, and close_node, not fail, any"
    " step a new brief replaces."
)
WAKE_CHILD_PAUSED_ITSELF = (
    "[CORSIA plan] The child delegated for {title} is paused on the user's"
    " word, so the work has stopped and no return is coming until the user"
    " resumes it. Only the user resumes; tell them if this step is needed now."
)
#: Rule 1 (#1970): a Turn running when its plan is paused is told, not killed.
#: ``{how}`` names the pause that reached a worker from the agent above it.
WAKE_PAUSED = (
    "[CORSIA plan] Your plan was paused{how} while this Turn was running."
    " Stop at the next safe point: finish or back out the step in hand, note in"
    " your plan where you stopped, and end this Turn. Start nothing new."
)
PAUSED_WITH_PARENT = " together with the agent that started you"
#: Rule 5 (#1973): every input a paused agent receives opens with this line,
#: the same for every harness and model. Rule 4 (#1975) will enforce it.
PAUSED_LINE = (
    "[Paused by the user since {since}. In this Turn do only what the user's own "
    "message here asks; otherwise read, reply and update your plan, and change "
    "nothing else. Only the user resumes.]"
)


def paused_line(since: str) -> str:
    """Return the line opening an input stamped with its plan's pause time."""
    return PAUSED_LINE.format(since=since[:16].replace("T", " ") + " UTC") + "\n\n"


def ask_release_summary(
    *, ask: str, done: str, retired: bool, kind: str, title: str, node_id: str,
    verdict, note, other_holds: bool,
) -> str:
    """Return the one-line answer to an ask, naming only the fields the user gave."""
    note = " ".join(str(note).split()) if note else ""
    return ASK_RELEASE_SUMMARY.format(
        event=(ASK_UNAVAILABLE if retired else ASK_ANSWERED).format(done=done, ask=ask),
        kind=kind, title=title, node_id=node_id,
        verdict=ASK_RELEASE_VERDICT.format(verdict=str(verdict).replace("_", " ")) if verdict else "",
        note=ASK_RELEASE_NOTE.format(note=note) if note else "",
        other="yes" if other_holds else "no",
    )


#: One step's own friction crossed the threshold: named once, at the step
#: that hit it, however many further attempts follow.
WAKE_CHILD_FRICTION_THRESHOLD = (
    "[CORSIA plan] {title} has reached attempt {attempts} without succeeding."
    " Check whether the brief, scope or model fits, then re-brief, split, or"
    " stop it."
)
#: A child's Turn itself failed (no A-row event says so) for a reason other
#: than a provider limit, and work is still open. A model switch would not
#: help, so the wake does not suggest one.
WAKE_CHILD_TURN_ERRORED = (
    "[CORSIA plan] The child delegated for {title} ended its Turn in error:"
    " {error} Its work is still open. Read what it last did with"
    " delegate_status, then tell it how to continue with delegate_send,"
    " re-brief it, or stop it."
)
#: The child cannot run on its model: the provider refused it (usage, session,
#: quota or credit limit, #2148) or the model is unavailable and its input is
#: held (#3317). Its work and history are intact, so the recovery is the same
#: thread on a model that does not share the limit.
WAKE_CHILD_PROVIDER_REFUSED = (
    "[CORSIA plan] The child delegated for {title} cannot run on"
    " {model}: {error} Its plan and history are intact. To continue now,"
    " switch that same thread with delegate_set_model to an eligible model"
    " that does not share this limit, then tell it to resume with"
    " delegate_send. {resume_note}"
)
REFUSED_RESUMES_AT = "Otherwise it resumes on its own at {at:%Y-%m-%d %H:%M} UTC."
REFUSED_NO_RESET = (
    "No reset time is known, so on this model it may be refused again."
)
REFUSED_HELD = "Otherwise it resumes on its own once that model is ready again."
#: The child ended a Turn with work it could still move and no other wake
#: told the parent why -- often a question for the parent (#2539). It may
#: have moved its plan in that Turn; nobody is working the rest now. A child
#: with an armed watch or a block on open work is waiting on that, not on the
#: parent, and is never told this (#2637).
WAKE_CHILD_STOPPED_OPEN = (
    "[CORSIA plan] The child delegated for {title} stopped with its work"
    " still open and may be waiting on you. Read its last message"
    " (final_message below; if empty, delegate_status), then answer it with"
    " delegate_send, re-brief it, or stop it."
)
#: W5: the child's open work is held only by the user, so nothing wakes it.
WAKE_CHILD_STOPPED_ON_USER = (
    "[CORSIA plan] The child delegated for {title} stopped: its open work"
    " is held only by the user (\"{task}\": {reason}), so nothing will wake it."
    " Raise it to the user as a decision, clear it yourself if you can, or stop the child."
)
#: The child's Turn is still running but has made no model call and written
#: no message for a long time -- possibly hung, possibly just slow.
WAKE_CHILD_SILENT = (
    "[CORSIA plan] The child delegated for {title} has been silent for"
    " {minutes} minutes (no model call or message since {since} UTC) while its"
    " Turn is still running. Check it with delegate_status. If it is stuck,"
    " steer it with delegate_send or hand the step to a new worker with"
    " replace_spawned_thread; if it is just slow, leave it."
)
#: The child returned and its parent has not judged it: a returned worker is
#: idle until the parent accepts or rejects the step.
WAKE_VERDICT_OWED = (
    "[CORSIA plan] {title} ({step}) came back from {worker} {minutes} minutes ago"
    " and is still waiting for your verdict. If the work is verified, accept it"
    " (accept_with_follow_up for what is left, e.g. a merge, with any wait on the"
    " follow-up); otherwise reject it with feedback."
)
#: The child thread itself is gone (purged or deleted) while work was
#: still open, so the delegation has ended without a return.
WAKE_CHILD_THREAD_ENDED = (
    "[CORSIA plan] The child delegated for {title} was {reason} with work"
    " still open, so no return is coming. The delegation has ended;"
    " re-delegate the work if it still needs doing.{stranded_note}"
)
#: Appended when the gone child had itself delegated to workers, one clause
#: each saying what became of that worker (#2647: delete means delete).
WAKE_STRANDED_GRANDCHILDREN = " It had delegated to other workers: {workers}."
STRANDED_TOP_LEVEL = "{thread} is now a top-level agent and keeps its own work"
STRANDED_ORPHANED = "{thread} still exists with no parent; stop it if it is running"
#: Replacing a worker closed steps it had delegated; those workers still run.
REPLACE_OLD_WORKER = (
    "{thread} was replaced: its open steps were closed as replaced and it can no"
    " longer report on this step, but it may still be running. Stop it with"
    " delegate tools if it is."
)
REPLACE_STRANDED = (
    " It had delegated to {threads}, whose steps were closed the same way. To"
    " take one over, adopt it onto your own step: replace_spawned_thread (or"
    " attach_spawned_thread on an empty step) naming its thread id. Otherwise"
    " close the work and stop it."
)
#: Told once to a live worker when its step is handed to another worker.
WORKER_REPLACED = (
    "You were replaced on {step}: it now belongs to {replacement}. Stop working"
    " on it, and do not report on it."
)
#: The transcript chip lines for the two notices a worker reads about itself
#: (#2071); the agent's text above stays its own input.
WORKER_REPLACED_CHIP = "Step handed to another worker"
DECISION_DRAFTED_CHIP = "Drafted decision passed to the parent"
#: A user archived the child; reversible, so nothing was closed.
WAKE_CHILD_THREAD_ARCHIVED = (
    "[CORSIA plan] The child delegated for {title} was archived with work"
    " still open, so no return is coming while it stays archived. Its work"
    " is untouched: unarchive it to resume, or close or re-delegate the step."
)
#: A worker's own accountable blocker (B4): named by its reason, and the
#: parent is the only one who can clear it or hand it to the user.
WAKE_CHILD_BLOCKER_RAISED = (
    "[CORSIA plan] {title} raised a blocker on itself it cannot clear:"
    " {reason} Resolve it yourself, or raise it to the user as a decision."
)
#: A worker's plan decision, ready for exactly the parent-tier gate that
#: already exists (E1): the wake carries the draft so nothing need be fetched.
WAKE_CHILD_DECISION_DRAFTED = (
    "[CORSIA plan] The child delegated for {title} drafted a decision"
    " ({decision_title}) that only you can act on: parent_review then raise"
    " it to the user, or update/invalidate it and steer the child yourself."
    " The full draft follows."
)
#: What the transcript chip says for each plan input above, keyed by its
#: ``event_type``. The lines above are written for the agent and stay its own
#: input; the chip is one short line for the person reading the transcript.
WAKE_CHIPS = {
    "plan.task_closed": "Task closed by another actor",
    "plan.work_selected": "New current task",
    "plan.ask_released": "Artifact ask released work",
    "plan.child_returned": "Delegated work returned for your review",
    "plan.revision_requested": "Returned work was rejected",
    "plan.prerequisite_settled": "A task you depend on settled",
    "plan.conductor_task_settled": "A task you added settled",
    "plan.child_returned_nothing": "Delegated worker returned nothing",
    "plan.child_round_returned": "Delegated worker stopped and is waiting on you",
    "plan.child_step_failed": "A delegated step failed",
    "plan.child_paused_itself": "Delegated worker paused its plan",
    "plan.child_friction_threshold": "Delegated step keeps failing",
    "plan.child_turn_errored": "Delegated worker's turn ended in error",
    "plan.child_provider_refused": "Delegated worker hit a provider limit",
    "plan.child_held_local_only": "Delegated worker is held: local models only",
    "plan.child_stopped_open": "Delegated worker stopped with work open",
    "plan.child_stopped_on_user": "Delegated worker is held by the user",
    "plan.child_silent": "Delegated worker has gone silent",
    "plan.verdict_owed": "Delegated step awaits your verdict",
    "plan.child_thread_ended": "Delegated worker ended with work open",
    "plan.child_thread_archived": "Delegated worker was archived",
    "plan.child_blocker_raised": "Delegated step raised a blocker",
    "plan.child_needs_input": "Delegated worker needs input",
    "plan.child_decision_drafted": "Delegated worker drafted a decision",
    "plan.decision_answered": "Decision answered",
    "plan.paused": "Plan paused: stop at a safe point",
}

# Account notices retain the existing refusal event, with their own fixed copy.
ACCOUNT_HELD_CHIP = 'Delegated worker is waiting for its account'
ACCOUNT_PENDING_RESUME_CHIP = 'Delegated worker is waiting to resume'
ACCOUNT_READINESS_UNREAD = 'Its account readiness could not be read. Inspect the held work before taking action.'
ACCOUNT_PENDING_DETAIL = 'Its account is ready; the input is waiting for its claim to resume.'
# Who can take each action: the user through a control ('user'), a provider
# source check ('source'), or the agent as written ('agent').
AccountActor = Literal['user', 'source', 'agent']
ACCOUNT_HOLD_COPY: dict[str | tuple[str, bool], tuple[str, str, AccountActor]] = {
    ('account_signed_out', False): ('it is signed out', 'Sign in to the permitted account through the account controls.', 'user'),
    ('account_signed_out', True): ('it is signed out', "Sign in through the vendor's own CLI, then check the provider source or request its canonical refresh.", 'user'),
    'account_removed': ('the account was removed', 'Choose another permitted account if one is available; otherwise switch model.', 'user'),
    'account_project_denied': ('project policy does not permit the account', 'Restore the permitted project policy through an authorized control, or ask the user to restore it; otherwise the user can choose another permitted account if one is available, or switch model.', 'agent'),
    'project_policy_missing': ("the thread's project policy is missing", 'Restore the project or move the thread to a permitted project through an authorized control, or ask the user to do so.', 'agent'),
    ('account_model_not_discovered', False): ('the selected model has not been discovered for this account', 'Verify the signed-in account through the account controls to discover its models.', 'user'),
    ('account_model_not_discovered', True): ('the selected model has not been discovered for this account', 'Check the provider source or request its canonical refresh to discover its models.', 'source'),
    'account_runtime_pending': ("this account's runtime is not yet available", 'Inspect the held work and report the missing runtime capability; signing in again will not enable it.', 'agent'),
    ('account_login_unverified', False): ("the account's current sign-in could not be verified", 'Verify the signed-in account through the account controls to obtain fresh account evidence.', 'user'),
    ('account_login_unverified', True): ("the account's current sign-in could not be verified", 'Check the provider source or request its canonical refresh to obtain fresh account evidence.', 'source'),
    'codex_auth_projection_changed': ('the managed authentication link changed', 'Sign in again through the account controls so the old managed link can be cleaned up and fresh account evidence obtained.', 'user'),
    'codex_auth_source_missing': ('the authentication source is missing', 'Sign in again through the account controls to restore the source and obtain fresh account evidence.', 'user'),
}
ACCOUNT_SOURCE_CHECK_ACTION = ('Check the provider source or request its canonical refresh through an available scoped tool'
                               ' with prior authority, or tell the user that this check is needed.')
ACCOUNT_HELD_WAKE_TEMPLATE = (
    '[CORSIA plan] The child delegated for {title} has input held for its account on'
    ' {model}: {phrase} ({cause}). Its plan and history are intact. Read delegate_status'
    ' and its current plan to inspect the held work. {action}'
    ' The user can cancel the held input in the inbox. Tell the user exactly what is needed,'
    " or return that need as review evidence from your own assigned plan task to your parent if you have no user route;"
    ' continue your other authorized work, and retain this same queued brief.'
    ' Do not resend it or re-delegate it. Policy or source-check actions require an available'
    ' scoped tool and prior authority; otherwise ask the user to act. If changing this same'
    " own child's model is already authorized, delegate_set_model to an exact permitted ready"
    ' and usage-usable selector is supported only when its current account and policy are usable.'
    ' Inspect any pending operation with delegate_status until its actual completion or failure;'
    ' after completion let canonical recovery claim the retained input, without a replacement'
    ' delegate_send. A same-vendor target whose account is still held has no recovery promise.'
    ' This is not a queued account rebind, implicit fallback or permission to change a persistent peer.'
)
ACCOUNT_PENDING_RESUME_WAKE_TEMPLATE = (
    '[CORSIA plan] The child delegated for {title} still has account-held input on {model}.'
    ' Its account is ready; the input is waiting for its claim to resume. Its plan and history'
    ' are intact. Read delegate_status and its current plan to inspect the held work. Continue'
    ' your other authorized work and retain this same queued brief; canonical recovery must claim it.'
    ' Do not resend it, re-delegate it or change its model merely because the claim has not run.'
    ' The user can cancel the held input in the inbox. No recovery deadline is promised.'
)


def validate_account_hold_state(event_type, state) -> None:
    """Only a refused-child notice carries an account hold state, and only a known one."""
    if state is not None and (event_type != 'plan.child_provider_refused'
                              or state not in ('unready', 'pending_resume')):
        raise ValueError('invalid account hold notice state')


def account_wake_chip(event_type, state) -> str:
    """The transcript chip for a wake, naming the account state when it has one."""
    validate_account_hold_state(event_type, state)
    if state is None:
        return WAKE_CHIPS[event_type]
    return ACCOUNT_HELD_CHIP if state == 'unready' else ACCOUNT_PENDING_RESUME_CHIP


def _account_copy(readiness):
    if not readiness:
        return None
    cause, default = readiness['cause'], readiness['is_default']
    return ACCOUNT_HOLD_COPY.get((cause, default)) if default is not None and (
        cause, default) in ACCOUNT_HOLD_COPY else ACCOUNT_HOLD_COPY.get(cause)


def account_readiness_detail(readiness) -> str:
    """Live advisory detail, with no diagnostic identifier or guessed recovery route."""
    if readiness and readiness['ready']:
        return ACCOUNT_PENDING_DETAIL
    copy = _account_copy(readiness)
    return f'Its account is not ready: {copy[0]}. {copy[1]}' if copy else ACCOUNT_READINESS_UNREAD


def account_wake_fields(readiness, error):
    """Validated fixed cause/actions for the existing account refusal notice."""
    if readiness and readiness['ready']:
        return 'pending_resume', {}
    copy = _account_copy(readiness)
    if copy is None:
        return 'unready', {'phrase': 'its readiness could not be read',
                           'cause': 'account_readiness_unread', 'action': ACCOUNT_READINESS_UNREAD}
    phrase, action, actor = copy
    if actor == 'user':
        action = 'The user can ' + action[0].lower() + action[1:]
    elif actor == 'source':
        action = ACCOUNT_SOURCE_CHECK_ACTION
    return 'unready', {'phrase': phrase, 'cause': readiness['cause'], 'action': action}
