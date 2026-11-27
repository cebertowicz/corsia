<script lang="ts">
	import { setContext, untrack } from 'svelte';
	import { REVIEW_ANSWERED, REVIEW_REPLIES } from '$lib/reviewBarState.svelte';
	import { openActivityDocument } from './activityDocumentOpen';
	import { Button } from '$lib/components/ui';
	import { keepInView } from '$lib/actions/keepInView';
	import ErrorReference from '$lib/components/ui/ErrorReference.svelte';
	import ArtifactLeaf from '$lib/components/ArtifactLeaf.svelte';
	import ReviewBarShell, { ASKER_AT_REST } from '$lib/components/ReviewBarShell.svelte';
	import ActivityKindPanel from './ActivityKindPanel.svelte';
	import ActivityChatPane from './ActivityChatPane.svelte';
	import { isPickAny, OTHER_OPTION_ID, type AttentionDestination, type DecisionDetail } from '$lib/api/attentionQueue';
	import { markDocumentsRead, type ActivityQueueItem } from '$lib/api/activityQueue';
	import { docComments, commentLoads } from '$lib/stores/docComments';
	import { connectionState } from '$lib/stores/connection';
	import { unreadReplyCount } from '$lib/docUnread';
	import type { HeldVerb } from '$lib/api/heldReplies';
	import { newMutationId, sendAnswer } from '$lib/stores/attentionAnswer.svelte';
	import { emitProductEvent } from '$lib/product_events';
	import { agentsLabel, chatDetailOf, decisionOf, documentDetailOf, documentOf, heldOf, replyPosition, reviewOf,
		taskOf, taskSteps, type DecisionDraft, type DocumentLookup } from './activityItems';
	import { answerPressed, decideHeldPress, markTaskDonePress, SUCCEEDED, type PressAction, type Pressed } from './activityPress';

	/**
	 * The open item, in the pane the chat uses for documents. A document opens in Activity's own workspace (never a
	 * chat's), read for the chat it came from; every other kind gets the same frame. Each control settles the item
	 * through the call its own surface already uses; the list then re-reads, and the item leaves only when the server
	 * stops returning it. Every outcome of a press is reported here; nothing here writes the attention queue's state.
	 */
	let { item, workspaceKey, draft, onDraft, onClearDraft, noLonger = false, stale = false, allRead = false, seenAnswers, onActed, onKeep, onOpenChat, onCloseChat }: {
		item: ActivityQueueItem;
		workspaceKey: string;
		draft?: DecisionDraft;
		onDraft: (draft: DecisionDraft) => void;
		onClearDraft: () => void;
		noLonger?: boolean;
		stale?: boolean;
		/** The open document's replies were all read while it stayed open (elsewhere, or by Mark all read). */
		allRead?: boolean; seenAnswers?: Set<string>;
		/** Re-read the queue after an act landed. */
		onActed: () => Promise<void> | void;
		/** The server let the item go without doing what was pressed: keep it open, with the reason, until the person moves on. */
		onKeep: () => void;
		onOpenChat: (threadId: string, destination?: AttentionDestination) => void | Promise<void>; onCloseChat: () => void;
	} = $props();

	// Every band here, the document's own review band included, names the agent asking at rest.
	setContext(ASKER_AT_REST, true);
	setContext(REVIEW_ANSWERED, () => void onActed());

	const chat = $derived(chatDetailOf(item));
	const decision = $derived(decisionOf(item));
	const held = $derived(heldOf(item));
	const task = $derived(taskOf(item));
	const document = $derived(documentDetailOf(item));
	const doc = $derived(documentOf(item));
	const sourceThread = $derived(item.source.thread_id);
	// The open document keeps its original lookup context until the person moves on.
	const documentThread = untrack(() => item.source.thread_id);
	const who = $derived({ name: item.source.thread_label, id: item.source.thread_id ?? item.key });

	let sending = $state(false);
	const offline = $derived($connectionState !== 'connected');
	let failure = $state<{ text: string; reference?: string; retry: PressAction | null; final: boolean } | null>(null);
	/** The decision answer's retry key: kept across Try again so a retry can't answer twice. */
	let answerMutation: string | null = null;

	// Decision draft: nothing is chosen until the person chooses, even where an agent recommends an option.
	let selected = $state<string | null>(untrack(() => draft?.selected ?? null));
	let picked = $state<string[]>(untrack(() => [...(draft?.picked ?? [])]));
	let other = $state(untrack(() => draft?.other ?? ''));
	let fieldFocused = $state(false);
	const rationaleOptionId = $derived(decision?.options.find((option) =>
		Array.isArray(decision.recommendation) ? decision.recommendation.includes(option.id) : decision.recommendation === option.id,
	)?.id ?? null);
	function saveDraft() { onDraft({ selected, picked: [...picked], other }); }
	let lookup = $state<DocumentLookup | { status: 'resolving' } | null>(null);

	// A document item opens in Activity's workspace as its only tab; the item it replaces is closed through the
	// ordinary close (which saves a pending edit and sends the open comment batch).
	// Primitive inputs only: a re-read hands over new item objects, and the document must not reopen for that.
	const docArtifactId = $derived(doc?.artifactId ?? null);
	// A document row lands on its newest unread reply. Fixed when the pane opens: an approved review that stays open as
	// its document must not land again.
	const landsOnReplies = untrack(() => item.kind === 'document');
	const replyThread = untrack(() => doc?.commentId);
	$effect(() => openActivityDocument(
		workspaceKey, docArtifactId, documentThread, landsOnReplies, replyThread,
		(value) => { lookup = value; },
	));

	/**
	 * Runs one press: busy while it runs, then the list re-reads, or the reason shows under the buttons. A refusal
	 * re-reads too, since the item changed or left; one that left stays open here until the person moves on.
	 */
	async function press(action: PressAction, verb: string, run: () => Promise<Pressed>) {
		if (sending) return;
		sending = true;
		failure = null;
		let pressed: Pressed;
		try {
			pressed = await run();
		} catch {
			pressed = { outcome: 'failed', text: `Couldn’t ${verb}. Try again.` };
		}
		emitProductEvent('activity', 'acted', {
			kind: item.kind, action, outcome: pressed.outcome, ...(pressed.outcome === 'refused' ? { reason: pressed.reason } : {}),
		});
		try {
			if (pressed.outcome === 'failed') {
				failure = { text: pressed.text, reference: pressed.reference, retry: action, final: false };
				return;
			}
			if (pressed.outcome === 'refused') {
				if (pressed.text) failure = { text: pressed.text, retry: null, final: pressed.final ?? false };
				if (pressed.final) onKeep();
			}
			await onActed();
		} finally {
			sending = false;
		}
	}

	const answerReady = $derived.by(() => {
		if (!decision) return false;
		if (isPickAny(decision)) return true;
		if (!selected) return false;
		return selected !== OTHER_OPTION_ID || other.trim().length > 0;
	});

	function answer(d: DecisionDetail) {
		return press('answer', 'save your answer', async () => {
			answerMutation ??= newMutationId();
			const outcome = await sendAnswer(d, {
				selected: isPickAny(d) ? null : selected,
				picked: isPickAny(d) ? [...picked] : null,
				other,
			}, answerMutation);
			if (outcome.status === 'ok') onClearDraft();
			if (outcome.status === 'ok' || outcome.status === 'gone') answerMutation = null;
			return answerPressed(outcome);
		});
	}

	function togglePicked(id: string) {
		picked = picked.includes(id) ? picked.filter((x) => x !== id) : [...picked, id];
		saveDraft();
	}

	const decide = (verb: HeldVerb) => verb === 'approve'
		? press('send', 'send this message', async () => (held ? decideHeldPress(held.id, verb) : SUCCEEDED))
		: press('dont_send', 'decline this message', async () => (held ? decideHeldPress(held.id, verb) : SUCCEEDED));
	const markTaskDone = () => press('done', 'mark this task done', async () => (task ? markTaskDonePress(task) : SUCCEEDED));
	/** Mark only the replies represented by the displayed content. */
	function markRepliesRead(through?: string) {
		if (!document && !(chat && through)) return;
		return press('done', 'mark these replies read', async () => {
			await markDocumentsRead(document ? [{ artifact_id: document.artifact_id, through_reply_id: document.latest_reply_id }] : [], chat && through ? [{ thread_id: chat.thread_id, through }] : []);
			return SUCCEEDED;
		});
	}

	/**
	 * Where the person is in this document's replies to their own comments — the set the row counted, never an
	 * unrelated unread comment. The total is fixed when the document opens and rises only as replies arrive.
	 */
	const serverUnread = $derived(document?.unread ?? reviewOf(item)?.unread_replies ?? 0);
	let total = $state(untrack(() => serverUnread));
	let lastServer = untrack(() => serverUnread);
	$effect(() => {
		const now = serverUnread;
		untrack(() => {
			if (now > lastServer) total += now - lastServer;
			lastServer = now;
		});
	});
	const commentsReady = $derived(docArtifactId !== null && $commentLoads[docArtifactId]?.state === 'ready');
	const liveUnread = $derived(docArtifactId && commentsReady ? unreadReplyCount($docComments[docArtifactId] ?? []) : null);
	const position = $derived(replyPosition(total, liveUnread ?? total));
	const repliesRead = $derived(allRead || liveUnread === 0);
	const positionLabel = $derived(position.of === 1 ? '1 reply' : `${position.at} of ${position.of} replies`);
	const barLabel = $derived(
		repliesRead ? 'All replies read.' : position.of === 1 ? '1 reply to your comment' : `${positionLabel} to your comments`,
	);
	// A review with unread replies says where the person is in them beside its ask.
	setContext(REVIEW_REPLIES, () => (total > 0 && !repliesRead && item.kind === 'review' ? positionLabel : ''));

	/** The button whose press failed reads Try again; after a refusal or a final outcome, every button keeps its name. */
	const labelFor = (action: PressAction, label: string) => (failure?.retry === action ? 'Try again' : label);
	/** An item the server has let go can't be acted on again. */
	const settled = $derived(failure?.final ?? false);
	const lookupFailed = $derived(lookup?.status === 'missing' || lookup?.status === 'failed');
</script>

{#snippet itemNotice()}
	{#if stale}<p class="no-longer" role="status">Showing last known items.</p>{/if}
	{#if noLonger}<p class="no-longer" role="status">No longer waiting on you.</p>{/if}
{/snippet}

{#snippet documentNotice()}
	{@render itemNotice()}
	{#if document}
		<section class="doc-bar" aria-label="Replies to your comments" aria-busy={sending}>
			<strong class="doc-who">{agentsLabel(document.agents)}</strong>
			<div class="doc-line">
				<span class="doc-at" role="status">{barLabel}</span>
				<span class="doc-done">
					{#if !repliesRead}<Button size="sm" variant="primary" loading={sending} onclick={() => void markRepliesRead()}>{labelFor('done', 'Done')}</Button>{/if}
				</span>
			</div>
			{#if failure}<p class="act-failure" role="alert">{failure.text}</p>{/if}
		</section>
	{/if}
{/snippet}

{#snippet failureLine()}
	<!-- One flex item, so the reference sits under its line on the band's wrapping row, not beside the buttons. -->
	{#if failure}<div class="act-failure-row"><p class="act-failure" role="alert">{failure.text}</p>{#if failure.reference}<ErrorReference reference={failure.reference} />{/if}</div>
	{:else if held?.error}<!-- Why the last Send didn't go, shown here until the next press. --><div class="act-failure-row"><p class="act-failure">{held.error}</p>{#if held.error_ref}<ErrorReference reference={held.error_ref} />{/if}</div>{/if}
{/snippet}

{#if chat}
	<ActivityChatPane {item} detail={chat} {allRead} {seenAnswers} {sending} readFailed={failure?.retry === 'done'} onDone={markRepliesRead} {onOpenChat} onClose={onCloseChat} />
{:else if doc}
	{#if lookupFailed}
		<ActivityKindPanel notice={noLonger || stale ? itemNotice : undefined} label={item.title}>
			{#snippet body()}
				<div class="unopenable" role="status">
					<h1 class="dh">Couldn’t open this document.</h1>
					<p class="dctx">
						{lookup?.status === 'failed'
							? 'Its chat’s files didn’t load.'
							: 'It isn’t in the files of the chat it came from; it may have been moved or removed.'}
					</p>
					{#if sourceThread}
						<Button size="sm" variant="secondary" onclick={() => onOpenChat(sourceThread)}>Open {item.source.thread_label}’s chat</Button>
					{/if}
				</div>
			{/snippet}
		</ActivityKindPanel>
	{:else if lookup?.status !== 'found'}
		<div class="doc-pane" aria-busy="true"></div>
	{:else}
		<div class="doc-pane">
			<ArtifactLeaf threadId={workspaceKey} targetThreadId={documentThread} notice={document || noLonger || stale ? documentNotice : undefined} protectReplyLanding={landsOnReplies} collapsible={false} mode="expanded" />
		</div>
	{/if}
{:else if decision}
	<ActivityKindPanel notice={noLonger || stale ? itemNotice : undefined} label="Decision">
		{#snippet body()}
			<h1 class="dh">{decision.title}</h1>
			{#if !decision.source.parent_reviewed}<p class="dctx">Raised directly — no parent review.</p>{/if}
			{#if decision.context}<p class="dctx">{decision.context}</p>{/if}
			{#if decision.tradeoff}<p class="dctx">{decision.tradeoff}</p>{/if}
			{#if !rationaleOptionId && decision.rationale}<dl class="decision-reason"><div><dt>Why</dt><dd>{decision.rationale}</dd></div></dl>{/if}
			<fieldset class="options">
				<legend class="sr-only">{isPickAny(decision) ? 'Choose any' : 'Choose one'}</legend>
				{#each decision.options as option (option.id)}
					{@const recommended = Array.isArray(decision.recommendation) ? decision.recommendation.includes(option.id) : decision.recommendation === option.id}
					{@const rationaleId = option.id === rationaleOptionId && decision.rationale ? `decision-${decision.id}-${option.id}-rationale` : undefined}
					<label class="opt">
						{#if isPickAny(decision)}
							<input type="checkbox" checked={picked.includes(option.id)} aria-describedby={rationaleId} onchange={() => togglePicked(option.id)} disabled={sending || offline} />
						{:else}
							<input type="radio" name="decision-{decision.id}" value={option.id} checked={selected === option.id} aria-describedby={rationaleId} onchange={() => { selected = option.id; saveDraft(); }} disabled={sending || offline} />
						{/if}
						<span><b>{option.label}</b>{#if recommended} <em>Recommended</em>{/if}{#if option.detail}<span class="opt-detail">{option.detail}</span>{/if}{#if rationaleId}<span class="option-rationale" id={rationaleId}><span>Why</span>{decision.rationale}</span>{/if}</span>
					</label>
				{/each}
				{#if decision.other_enabled && !isPickAny(decision)}
					<label class="opt">
						<input type="radio" name="decision-{decision.id}" value={OTHER_OPTION_ID} checked={selected === OTHER_OPTION_ID} onchange={() => { selected = OTHER_OPTION_ID; saveDraft(); }} disabled={sending || offline} />
						<span><b>Something else</b></span>
					</label>
				{/if}
				{#if decision.other_enabled && (selected === OTHER_OPTION_ID || isPickAny(decision))}
					<textarea class="other" rows="3" use:keepInView={{ enabled: fieldFocused }} onfocus={() => { fieldFocused = true; }} onblur={() => { fieldFocused = false; }} bind:value={other} oninput={(event) => { other = event.currentTarget.value; saveDraft(); }} disabled={sending || offline}
						placeholder={isPickAny(decision) ? 'Add a note (optional)' : 'Say what you want instead'}
						aria-label={isPickAny(decision) ? 'Note' : 'Your answer'}></textarea>
				{/if}
			</fieldset>
		{/snippet}
		{#snippet band()}
			<ReviewBarShell view="ask" {who} full="Waiting for your answer" askLabel="Decision" docked busy={sending}>
				{#snippet actions()}
					<div class="rb-actions">
						<Button size="sm" variant="primary" loading={sending} disabled={!answerReady || settled || offline} onclick={() => void answer(decision)}>{labelFor('answer', 'Answer')}</Button>
					</div>
					{@render failureLine()}
					{#if offline}<p class="act-failure-row offline-notice" role="status">Answering is paused until this reconnects.</p>{/if}
				{/snippet}
			</ReviewBarShell>
		{/snippet}
	</ActivityKindPanel>
{:else if task}
	<ActivityKindPanel notice={noLonger || stale ? itemNotice : undefined} label="Task">
		{#snippet body()}
			<h1 class="dh">{item.title}</h1>
			{#if task.description}{@const steps = taskSteps(task.description)}
				{#if steps.kind === 'text'}<p class="body pre">{steps.text}</p>
				{:else}<svelte:element this={steps.kind === 'numbered' ? 'ol' : 'ul'} class="body steps" start={steps.kind === 'numbered' ? steps.start : undefined}>{#each steps.items as step, i (i)}<li>{step}</li>{/each}</svelte:element>{/if}
			{/if}
		{/snippet}
		{#snippet band()}
			<ReviewBarShell view="ask" {who} full="A step only you can do" askLabel="Task for you" docked busy={sending}>
				{#snippet actions()}
					<div class="rb-actions">
						<Button size="sm" variant="primary" loading={sending} onclick={() => void markTaskDone()}>{labelFor('done', 'Done')}</Button>
					</div>
					{@render failureLine()}
				{/snippet}
			</ReviewBarShell>
		{/snippet}
	</ActivityKindPanel>
{:else if held}
	<ActivityKindPanel notice={noLonger || stale ? itemNotice : undefined} label="Message">
		{#snippet body()}
			<h1 class="dh">{item.title}</h1>
			<p class="dctx">To: {held.provider ? `${held.recipient} on ${held.provider}` : held.recipient}</p>
			<blockquote class="pre">{held.text}</blockquote>
		{/snippet}
		{#snippet band()}
			<ReviewBarShell view="ask" {who} full="Waiting for your yes before it’s sent" askLabel="Message waiting for your yes" docked busy={sending}>
				{#snippet actions()}
					<div class="rb-actions">
						<Button size="sm" variant="ghost" disabled={sending || settled || offline} onclick={() => void decide('decline')}>{labelFor('dont_send', 'Don’t send')}</Button>
						<Button size="sm" variant="primary" loading={sending} disabled={settled || offline} onclick={() => void decide('approve')}>{labelFor('send', 'Send')}</Button>
					</div>
					{@render failureLine()}
					{#if offline}<p class="act-failure-row offline-notice" role="status">Sending and declining are paused until this reconnects.</p>{/if}
				{/snippet}
			</ReviewBarShell>
		{/snippet}
	</ActivityKindPanel>
{:else}
	<ActivityKindPanel notice={noLonger || stale ? itemNotice : undefined} label={item.title}>
		{#snippet body()}
			<h1 class="dh">{item.title}</h1>
			{#if sourceThread}
				<Button size="sm" variant="secondary" onclick={() => onOpenChat(sourceThread)}>Open {item.source.thread_label}’s chat</Button>
			{/if}
		{/snippet}
	</ActivityKindPanel>
{/if}

<style>
	.no-longer { margin: 0; padding: var(--space-2) var(--space-5); color: var(--text-secondary); font-size: var(--text-subhead); }
	.doc-pane { position: relative; height: 100%; display: flex; flex-direction: column; min-width: 0; min-height: 0; --comment-jump-margin: var(--space-8); }
	/* The document bar is in the leaf's notice slot, just below its tab strip. */
	/* The document's bar: who replied, then where the person is in the replies with Done on the same line. Done's
	   place keeps its height once the replies are read, so the document does not jump. */
	.doc-bar {
		display: flex; flex-direction: column; gap: var(--space-1);
		padding: var(--space-3) var(--space-5);
		border-bottom: 1px solid var(--border-subtle);
		background: var(--bg-primary); font-size: var(--text-footnote); color: var(--text-secondary);
	}
	.doc-who { color: var(--text-primary); font-size: var(--text-subhead); font-weight: var(--fw-semibold); overflow-wrap: anywhere; }
	.doc-line { display: flex; align-items: center; gap: var(--space-3); min-height: var(--space-7); }
	.doc-at { flex: 1; min-width: 0; font-size: var(--text-subhead); }
	.doc-done { flex: none; display: inline-flex; }
	:global(.activity.phone) .doc-bar { padding-inline: var(--space-3); }
	:global(.activity.phone) .doc-done :global(.btn) { min-height: var(--touch-target); }
	:global(.activity.phone) .doc-line { min-height: var(--touch-target); }
	.dh { margin: 0 0 var(--space-3); font-size: var(--text-title3); line-height: 1.35; color: var(--text-primary); }
	/* Every kind's body reads at the document body's size and spacing (cm/theme.ts: 15px = --text-body; --text-input on a phone). */
	.dctx, .body, blockquote, .opt { font-size: var(--text-body); line-height: var(--lh-relaxed); }
	:global(.activity.phone) :is(.dctx, .body, blockquote, .opt) { font-size: var(--text-input); }
	.dctx { margin: 0 0 var(--space-3); color: var(--text-secondary); }
	.body { margin: 0 0 var(--space-3); color: var(--text-primary); }
	.steps { padding-left: var(--space-5); }
	.steps li + li { margin-top: var(--space-1); }
	.pre { white-space: pre-wrap; }
	blockquote {
		margin: var(--space-4) 0; padding: 0 0 0 var(--space-4); border-left: 1px solid var(--border-subtle);
		color: var(--text-primary);
	}
	.options { margin: var(--space-3) 0 0; padding: 0; border: 0; }
	.opt { display: flex; gap: var(--space-3); padding: var(--space-3) 0; cursor: pointer; }
	.opt input { flex: none; margin: var(--space-px-2) 0 0; accent-color: var(--accent); }
	.opt b { font-weight: var(--fw-semibold); color: var(--text-primary); }
	.opt em { margin-left: var(--space-2); font-style: normal; color: var(--accent-text); font-size: var(--text-footnote); }
	.opt-detail { display: block; margin-top: var(--space-1); color: var(--text-secondary); }
	.option-rationale { display: flex; gap: var(--space-1); margin: 0; color: var(--text-secondary); font-size: var(--text-footnote); line-height: var(--lh-snug); }
	.option-rationale span { flex: none; color: var(--text-tertiary); font-weight: var(--fw-semibold); }
	.decision-reason { display: grid; gap: var(--space-2); margin: 0; }
	.decision-reason div { padding: var(--space-2) var(--space-3); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--bg-secondary); }
	.decision-reason dt { margin-bottom: var(--space-1); color: var(--text-tertiary); font-size: var(--text-footnote); font-weight: var(--fw-semibold); }
	.decision-reason dd { margin: 0; color: var(--text-secondary); font-size: var(--text-footnote); line-height: var(--lh-normal); }
	.other {
		width: 100%; box-sizing: border-box; margin-top: var(--space-1); padding: var(--space-2);
		border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--bg-primary);
		color: var(--text-primary); font: inherit; font-size: var(--text-subhead); resize: vertical;
	}
	.unopenable { padding-top: var(--space-4); }
	.act-failure-row { flex: 1 0 100%; order: 10; text-align: right; }
	.offline-notice { margin: 0; color: var(--text-secondary); font-size: var(--text-footnote); }
	.act-failure { margin: 0; color: var(--red-text); font-size: var(--text-footnote); }
	.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
</style>
