<script lang="ts">
	import { setContext, untrack } from 'svelte';
	import { REVIEW_ANSWERED } from '$lib/reviewBarState.svelte';
	import { get } from 'svelte/store';
	import { Button } from '$lib/components/ui';
	import ErrorReference from '$lib/components/ui/ErrorReference.svelte';
	import ArtifactLeaf from '$lib/components/ArtifactLeaf.svelte';
	import ReviewBarShell, { ASKER_AT_REST } from '$lib/components/ReviewBarShell.svelte';
	import ActivityKindPanel from './ActivityKindPanel.svelte';
	import { isPickAny, OTHER_OPTION_ID, type DecisionDetail } from '$lib/api/attentionQueue';
	import type { ActivityQueueItem } from '$lib/api/activityQueue';
	import { markCommentThreadViewed } from '$lib/api/docs';
	import type { HeldVerb } from '$lib/api/heldReplies';
	import { newMutationId, sendAnswer } from '$lib/stores/attentionAnswer.svelte';
	import { emitProductEvent } from '$lib/product_events';
	import { requestArtifactTabClose } from '$lib/artifactTabClose';
	import { openArtifactTab, threadWorkspaces } from '$lib/stores/artifactWorkspace';
	import {
		decisionOf, docReplyOf, documentOf, findDocument, heldOf, taskOf, taskSteps,
		type DocumentLookup,
	} from './activityItems';
	import {
		answerPressed, decideHeldPress, markTaskDonePress, SUCCEEDED, type PressAction, type Pressed,
	} from './activityPress';

	/**
	 * The open item, in the pane the chat uses for documents. A document opens in Activity's own workspace (never a
	 * chat's), read for the chat it came from; every other kind gets the same frame. Each kind's control sits in the
	 * docked band at the bottom and settles the item through the call its own surface already uses; the list then
	 * re-reads, and the item leaves only when the server stops returning it. Every outcome of a press is reported here,
	 * under the band's buttons; nothing here writes the dock's state.
	 */
	let { item, workspaceKey, noLonger = false, onActed, onKeep, onOpenChat }: {
		item: ActivityQueueItem;
		workspaceKey: string;
		noLonger?: boolean;
		/** Re-read the queue after an act landed. */
		onActed: () => Promise<void> | void;
		/** The server let the item go without doing what was pressed: keep it open, with the reason, until the person moves on. */
		onKeep: () => void;
		onOpenChat: (threadId: string) => void;
	} = $props();

	// Every band here, the document's own review band included, names the agent asking at rest.
	setContext(ASKER_AT_REST, true);
	setContext(REVIEW_ANSWERED, () => void onActed());

	const decision = $derived(decisionOf(item));
	const held = $derived(heldOf(item));
	const task = $derived(taskOf(item));
	const docReply = $derived(docReplyOf(item));
	const doc = $derived(documentOf(item));
	const sourceThread = $derived(item.source.thread_id);
	// The open document keeps its original lookup context until the person moves on.
	const documentThread = untrack(() => item.source.thread_id);
	const who = $derived({ name: item.source.thread_label, id: item.source.thread_id ?? item.key });

	let sending = $state(false);
	let failure = $state<{ text: string; reference?: string; retry: PressAction | null; final: boolean } | null>(null);
	/** The decision answer's retry key: kept across Try again so a retry can't answer twice. */
	let answerMutation: string | null = null;

	// Decision draft: nothing is chosen until the person chooses, even where an agent recommends an option.
	let selected = $state<string | null>(null);
	let picked = $state<string[]>([]);
	let other = $state('');

	let lookup = $state<DocumentLookup | { status: 'resolving' } | null>(null);

	// The parent remounts this pane only when the person opens another item.

	// A document item opens in Activity's workspace as its only tab; the item it replaces is closed through the
	// ordinary close (which saves a pending edit and sends the open comment batch).
	// Primitive inputs only: a re-read hands over new item objects, and the document must not reopen for that.
	const docArtifactId = $derived(doc?.artifactId ?? null);
	// A queue key may change while the same document stays open.
	const landsOnReplies = $derived(item.kind === 'reply');
	// A reply row opens on the thread it quotes, never the document's first unread one.
	const replyThread = $derived(docReply?.root_id ?? doc?.commentId);
	$effect(() => {
		const artifactId = docArtifactId;
		const thread = documentThread;
		// Resolve by document identity, not the queue row's key.
		const landOnReplies = landsOnReplies;
		const commentId = replyThread;
		if (!artifactId) {
			lookup = null;
			void closeAllBut(null);
			return;
		}
		if (!thread) {
			lookup = { status: 'missing' };
			return;
		}
		lookup = { status: 'resolving' };
		let current = true;
		void findDocument(artifactId).then((found) => {
			if (!current) return;
			lookup = found;
			if (found.status !== 'found') return;
			openArtifactTab(workspaceKey, {
				artifactId,
				path: found.path,
				title: found.title ?? found.path.split('/').pop() ?? found.path,
				...(landOnReplies ? { landOn: 'new-replies' as const, landOnCommentId: commentId } : {}),
			});
			const open = get(threadWorkspaces)[workspaceKey]?.activeTabId ?? null;
			void closeAllBut(open);
		});
		return () => {
			current = false;
		};
	});

	async function closeAllBut(keep: string | null) {
		const tabs = get(threadWorkspaces)[workspaceKey]?.tabs ?? [];
		for (const tab of tabs) {
			if (tab.instanceId !== keep) await requestArtifactTabClose(workspaceKey, tab, () => {});
		}
	}

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
			if (outcome.status === 'ok' || outcome.status === 'gone') answerMutation = null;
			return answerPressed(outcome);
		});
	}

	function togglePicked(id: string) {
		picked = picked.includes(id) ? picked.filter((x) => x !== id) : [...picked, id];
	}

	const decide = (verb: HeldVerb) => verb === 'approve'
		? press('send', 'send this message', async () => (held ? decideHeldPress(held.id, verb) : SUCCEEDED))
		: press('dont_send', 'decline this message', async () => (held ? decideHeldPress(held.id, verb) : SUCCEEDED));
	const markTaskDone = () => press('done', 'mark this task done', async () => (task ? markTaskDonePress(task) : SUCCEEDED));
	const markReplyRead = () => press('done', 'mark this reply read', async () => {
		if (docReply) await markCommentThreadViewed(docReply.artifact_id, docReply.root_id, docReply.reply_id);
		return SUCCEEDED;
	});

	/**
	 * Reply lands the open document on the reply again, with its comment box open under the thread, as in a document
	 * today. The tab stays open: closing it would send the person's unsent comments.
	 */
	function reply() {
		if (lookup?.status !== 'found' || !doc) return;
		openArtifactTab(workspaceKey, {
			artifactId: doc.artifactId, path: lookup.path, title: lookup.title ?? lookup.path.split('/').pop() ?? lookup.path, landOn: 'new-replies', landOnCommentId: replyThread,
		});
	}

	/** The button whose press failed reads Try again; after a refusal or a final outcome, every button keeps its name. */
	const labelFor = (action: PressAction, label: string) => (failure?.retry === action ? 'Try again' : label);
	/** An item the server has let go can't be acted on again. */
	const settled = $derived(failure?.final ?? false);
	const lookupFailed = $derived(lookup?.status === 'missing' || lookup?.status === 'failed');
</script>

{#snippet goneNotice()}<p class="no-longer" role="status">No longer waiting on you.</p>{/snippet}

{#snippet failureLine()}
	<!-- One flex item, so the reference sits under its line on the band's wrapping row, not beside the buttons. -->
	{#if failure}<div class="act-failure-row"><p class="act-failure" role="alert">{failure.text}</p>{#if failure.reference}<ErrorReference reference={failure.reference} />{/if}</div>
	{:else if held?.error}<!-- Why the last Send didn't go, as the dock shows it until the next press. --><div class="act-failure-row"><p class="act-failure">{held.error}</p>{#if held.error_ref}<ErrorReference reference={held.error_ref} />{/if}</div>{/if}
{/snippet}

{#if doc}
	{#if lookupFailed}
		<ActivityKindPanel notice={noLonger ? goneNotice : undefined} label={item.title}>
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
			<ArtifactLeaf threadId={workspaceKey} targetThreadId={documentThread} notice={noLonger ? goneNotice : undefined} collapsible={false} mode="expanded" />
			{#if docReply}
				<div class="doc-band">
					<ReviewBarShell view="ask" {who} full="Replied to your comment" askLabel="Reply to your comment" docked busy={sending}>
						{#snippet actions()}
							<div class="rb-actions">
								<Button size="sm" variant="ghost" disabled={sending} onclick={() => void reply()}>Reply</Button>
								<Button size="sm" variant="primary" loading={sending} onclick={() => void markReplyRead()}>{labelFor('done', 'Done')}</Button>
							</div>
							{@render failureLine()}
						{/snippet}
					</ReviewBarShell>
				</div>
			{/if}
		</div>
	{/if}
{:else if decision}
	<ActivityKindPanel notice={noLonger ? goneNotice : undefined} label="Decision">
		{#snippet body()}
			<h1 class="dh">{decision.title}</h1>
			{#if decision.context}<p class="dctx">{decision.context}</p>{/if}
			{#if decision.tradeoff}<p class="dctx">{decision.tradeoff}</p>{/if}
			<fieldset class="options">
				<legend class="sr-only">{isPickAny(decision) ? 'Choose any' : 'Choose one'}</legend>
				{#each decision.options as option (option.id)}
					{@const recommended = Array.isArray(decision.recommendation) ? decision.recommendation.includes(option.id) : decision.recommendation === option.id}
					<label class="opt">
						{#if isPickAny(decision)}
							<input type="checkbox" checked={picked.includes(option.id)} onchange={() => togglePicked(option.id)} disabled={sending} />
						{:else}
							<input type="radio" name="decision-{decision.id}" value={option.id} checked={selected === option.id} onchange={() => (selected = option.id)} disabled={sending} />
						{/if}
						<span><b>{option.label}</b>{#if recommended} <em>Recommended</em>{/if}{#if option.detail}<span class="opt-detail">{option.detail}</span>{/if}</span>
					</label>
				{/each}
				{#if decision.other_enabled && !isPickAny(decision)}
					<label class="opt">
						<input type="radio" name="decision-{decision.id}" value={OTHER_OPTION_ID} checked={selected === OTHER_OPTION_ID} onchange={() => (selected = OTHER_OPTION_ID)} disabled={sending} />
						<span><b>Something else</b></span>
					</label>
				{/if}
				{#if decision.other_enabled && (selected === OTHER_OPTION_ID || isPickAny(decision))}
					<textarea class="other" rows="3" bind:value={other} disabled={sending}
						placeholder={isPickAny(decision) ? 'Add a note (optional)' : 'Say what you want instead'}
						aria-label={isPickAny(decision) ? 'Note' : 'Your answer'}></textarea>
				{/if}
			</fieldset>
		{/snippet}
		{#snippet band()}
			<ReviewBarShell view="ask" {who} full="Waiting for your answer" askLabel="Decision" docked busy={sending}>
				{#snippet actions()}
					<div class="rb-actions">
						<Button size="sm" variant="primary" loading={sending} disabled={!answerReady || settled} onclick={() => void answer(decision)}>{labelFor('answer', 'Answer')}</Button>
					</div>
					{@render failureLine()}
				{/snippet}
			</ReviewBarShell>
		{/snippet}
	</ActivityKindPanel>
{:else if task}
	<ActivityKindPanel notice={noLonger ? goneNotice : undefined} label="Task">
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
	<ActivityKindPanel notice={noLonger ? goneNotice : undefined} label="Message">
		{#snippet body()}
			<h1 class="dh">{item.title}</h1>
			<p class="dctx">To: {held.provider ? `${held.recipient} on ${held.provider}` : held.recipient}</p>
			<blockquote class="pre">{held.text}</blockquote>
		{/snippet}
		{#snippet band()}
			<ReviewBarShell view="ask" {who} full="Waiting for your yes before it’s sent" askLabel="Message waiting for your yes" docked busy={sending}>
				{#snippet actions()}
					<div class="rb-actions">
						<Button size="sm" variant="ghost" disabled={sending || settled} onclick={() => void decide('decline')}>{labelFor('dont_send', 'Don’t send')}</Button>
						<Button size="sm" variant="primary" loading={sending} disabled={settled} onclick={() => void decide('approve')}>{labelFor('send', 'Send')}</Button>
					</div>
					{@render failureLine()}
				{/snippet}
			</ReviewBarShell>
		{/snippet}
	</ActivityKindPanel>
{:else}
	<ActivityKindPanel notice={noLonger ? goneNotice : undefined} label={item.title}>
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
	.doc-pane { position: relative; height: 100%; display: flex; flex-direction: column; min-width: 0; min-height: 0; }
	/* The reply control docks over the document's bottom edge, where a review ask docks: placed one canvas pad in, so
	   the card still reaches the pane's edges and its content keeps the same inset as every other band. */
	.doc-band { --canvas-pad-inline: var(--space-5); position: absolute; left: var(--canvas-pad-inline); right: var(--canvas-pad-inline); bottom: 0; z-index: 5; }
	:global(.activity.phone) .doc-band { --canvas-pad-inline: var(--space-3); }
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
	.other {
		width: 100%; box-sizing: border-box; margin-top: var(--space-1); padding: var(--space-2);
		border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--bg-primary);
		color: var(--text-primary); font: inherit; font-size: var(--text-subhead); resize: vertical;
	}
	.unopenable { padding-top: var(--space-4); }
	.act-failure-row { flex: 1 0 100%; order: 10; text-align: right; }
	.act-failure { margin: 0; color: var(--red); font-size: var(--text-footnote); }
	.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
</style>
