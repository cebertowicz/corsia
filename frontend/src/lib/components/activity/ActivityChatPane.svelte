<script lang="ts">
	import { tick, untrack } from 'svelte';
	import ArtifactTabStrip from '$lib/components/ArtifactTabStrip.svelte';
	import DocJumpControl from '$lib/components/DocJumpControl.svelte';
	import type { ArtifactTab } from '$lib/artifactTabTypes';
	import { apiFetch } from '$lib/api/request';
	import { threads, isNavAgent } from '$lib/stores/threads';
	import type { ActivityChatDetail, ActivityQueueItem } from '$lib/api/activityQueue';
	import type { AttentionDestination } from '$lib/api/attentionQueue';
	import type { ChatComment } from '$lib/chatCommentTypes';
	import { formatMoment, parseUTC } from '$lib/time';
	import { renderMarkdown } from '$lib/markdown';
	import { chatTextPoint } from '$lib/chatCommentAnchors';
	import { wrapRange } from '$lib/cm/widget-paint';
	import { Avatar, Button } from '$lib/components/ui';
	import { agentColor } from '$lib/docPresence';

	let { item, detail, allRead = false, sending = false, readFailed = false, seenAnswers = new Set<string>(), onDone, onOpenChat, onClose }: {
		item: ActivityQueueItem; detail: ActivityChatDetail; allRead?: boolean; sending?: boolean; readFailed?: boolean;
		onDone: (through: string) => void | Promise<void>;
		onOpenChat: (threadId: string, destination?: AttentionDestination) => void | Promise<void>;
		onClose: () => void; seenAnswers?: Set<string>;
	} = $props();
	type ChatMessage = { id: string; seq: number; content: string; chat_visible_content?: string; created_at: string; chat_comments: ChatComment[] };
	const threadId = untrack(() => detail.thread_id);
	const latestThrough = $derived(detail.through);
	const agentName = $derived.by(() => {
		const current = $threads.find(thread => thread.id === detail.thread_id);
		return current && isNavAgent(current) ? current.title : 'CORSIA';
	});
	const tab = $derived<ArtifactTab>({ kind: 'subagent', instanceId: item.key, artifactId: null, path: '', title: item.title, threadId, loadedRevision: null, latestRevision: null, status: 'ready' });
	let scroll = $state<HTMLElement>();
	let jumpTarget = $state<{ count: number; direction: 'above' | 'below'; element: HTMLElement } | null>(null);
	let position = $state(1);
	function updateJump() {
		if (!scroll || allRead) { jumpTarget = null; return; }
		const box = scroll.getBoundingClientRect();
		const texts = [...scroll.querySelectorAll<HTMLElement>('[data-unread-answer-text]')];
		for (const text of texts) {
			const rect = text.getBoundingClientRect();
			if (rect.bottom > box.top && rect.top < box.bottom) seenAnswers.add(text.dataset.unreadAnswerText!);
		}
		const unseen = texts.filter(el => !seenAnswers.has(el.dataset.unreadAnswerText!));
		const below = unseen.filter(el => el.getBoundingClientRect().top >= box.bottom);
		const unseenAbove = unseen.filter(el => el.getBoundingClientRect().bottom <= box.top);
		const firstVisible = texts.findIndex(el => {
			const rect = el.getBoundingClientRect();
			return rect.bottom > box.top && rect.top < box.bottom;
		});
		const next = texts.findIndex(el => el.getBoundingClientRect().top >= box.bottom);
		const atEnd = scroll.scrollTop + scroll.clientHeight >= scroll.scrollHeight - 1;
		position = atEnd ? detail.unread : firstVisible >= 0 ? firstVisible + 1 : next >= 0 ? next + 1 : Math.max(1, texts.length);
		jumpTarget = below.length ? { count: unseen.length, direction: 'below', element: below[0] }
			: unseenAbove.length ? { count: unseen.length, direction: 'above', element: unseenAbove[unseenAbove.length - 1] } : null;
	}
	let messages = $state<ChatMessage[]>([]);
	$effect(() => {
		void messages; void allRead;
		const el = scroll;
		void tick().then(updateJump);
		if (!el || typeof ResizeObserver === 'undefined') return;
		const observer = new ResizeObserver(updateJump);
		observer.observe(el);
		return () => observer.disconnect();
	});
	let loading = $state(true), failed = $state(false), shownThrough = $state<string | null>(null);
	let loadVersion = 0;
	async function load(through: string) {
		const version = ++loadVersion;
		loading = untrack(() => messages.length === 0); failed = false;
		try {
			const response = await apiFetch(`/api/attention/notifications/chats/${encodeURIComponent(threadId)}`);
			if (!response.ok) throw new Error('read failed');
			const loaded = await response.json();
			if (version === loadVersion) { messages = loaded.messages; shownThrough = through; }
		} catch { if (version === loadVersion) failed = true; }
		finally { if (version === loadVersion) loading = false; }
	}
	$effect(() => { void load(latestThrough); });
	function jump(message: ChatMessage, comment: ChatComment) {
		void onOpenChat(detail.thread_id, { pane: 'chat-replies', id: message.id, seq: message.seq,
			cid: comment.cid, user_message_id: comment.user_message_id });
	}
	const when = (value: string) => formatMoment(parseUTC(value), new Date(), { clock: true, plain: true });
	function highlight(node: HTMLElement, comment: ChatComment) {
		if ((node.textContent ?? '').slice(comment.from_off, comment.to_off) !== comment.quote) return;
		const range = document.createRange();
		range.setStart(...chatTextPoint(node, comment.from_off));
		range.setEnd(...chatTextPoint(node, comment.to_off));
		wrapRange(range, { from: comment.from_off, to: comment.to_off, id: comment.id, kind: 'placed' }, false);
	}
</script>

<div class="chat-pane">
	<div class="strip"><ArtifactTabStrip tabs={[tab]} activeKey={tab.instanceId} onSelect={() => {}} onClose={onClose} /></div>
	<section class="bar" aria-label="Replies to your comments">
		<div class="who"><Avatar name={agentName} color={agentColor(threadId)} hidden /><div><strong>{agentName}</strong><div>{allRead ? 'All replies read.' : detail.unread === 1 ? '1 reply to your comment' : `${position} of ${detail.unread} replies to your comments`}</div></div></div>
		{#if !allRead}<Button size="sm" variant="primary" loading={sending} disabled={loading || failed || !shownThrough} onclick={() => shownThrough && void onDone(shownThrough)}>Done</Button>{/if}
	</section>
	{#if readFailed}<p role="alert">Couldn’t mark these replies read. Try again.</p>{/if}
	<div class="scroll" bind:this={scroll} onscroll={updateJump}>
		{#if loading}<p>Loading…</p>
		{:else if failed}<p role="alert">Couldn’t load these replies. <button type="button" onclick={() => void load(latestThrough)}>Try again</button></p>
		{:else}
			{#each messages as message (message.id)}
				{#each message.chat_comments as comment (`${comment.user_message_id}:${comment.cid}`)}
					<section class="conversation">
						<div class="meta"><span>{agentName} · {when(message.created_at)}</span><button type="button" onclick={() => jump(message, comment)}>Go to message ›</button></div>
						<!-- AssistantMessage includes a leading space in the rendered text that comment offsets count. -->
						<div class="message" use:highlight={comment}>{' '}{@html renderMarkdown(message.chat_visible_content ?? message.content)}</div>
						<div class="thread"><div><strong>You</strong> · {when(comment.created_at)}</div><div class="text">{@html renderMarkdown(comment.note)}</div>
							{#each comment.answers as answer}
								<div data-unread-answer-text={answer.unread && answer.author_kind === 'agent' ? `${answer.user_message_id}:${answer.cid}:${answer.assistant_message_id}` : undefined}>
									<div data-unread-answer={answer.unread ? '' : undefined}><span class:agent={answer.author_kind === 'agent'}>{answer.author_kind === 'agent' ? agentName : 'You'}</span> · {when(answer.created_at)}</div>
									<div class="text">{@html renderMarkdown(answer.text)}</div>
								</div>
							{/each}
						</div>
					</section>
				{/each}
			{/each}
		{/if}
	</div>
	{#if jumpTarget}<div class="jump"><DocJumpControl count={jumpTarget.count} direction={jumpTarget.direction} label={`${jumpTarget.count} new, next ${jumpTarget.direction}`} onJump={() => jumpTarget?.element.scrollIntoView({ block: 'center', behavior: 'smooth' })} /></div>{/if}
</div>

<style>
	.chat-pane { position: relative; height: 100%; min-height: 0; display: flex; flex-direction: column; padding-top: var(--header-bottom); background: var(--bg-primary); }
	.strip { position: absolute; inset: 0 var(--header-right-controls-inset, 0px) auto 0; height: var(--header-height); z-index: 11; overflow: hidden; }
	.jump { position: absolute; bottom: var(--space-4); left: var(--space-3); z-index: 2; }
	.bar { display: flex; justify-content: space-between; align-items: center; gap: var(--space-3); padding: var(--space-3) var(--space-5); border-bottom: 1px solid var(--border-subtle); color: var(--text-secondary); font-size: var(--text-subhead); }
	.bar strong { color: var(--text-primary); } .who { display: flex; align-items: flex-start; gap: var(--space-2); } .who > div > div { margin-top: var(--space-1); }
	.scroll { overflow-y: auto; padding: var(--space-5); padding-bottom: calc(var(--touch-target) + var(--space-4) + var(--space-3)); font-size: var(--text-body); line-height: var(--lh-normal); }
	.conversation + .conversation { margin-top: var(--space-6); }
	.meta { display: flex; justify-content: space-between; gap: var(--space-3); color: var(--text-tertiary); }
	.meta button { border: 0; padding: 0; background: none; color: var(--accent-text); font: inherit; cursor: pointer; white-space: nowrap; }
	.message { margin-top: var(--space-2); padding: var(--space-4); background: var(--bg-secondary); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); }
	.message :global(> :first-child) { margin-top: 0; }
	.message :global(> :last-child) { margin-bottom: 0; }
	.message :global(.cm-comment-wrap) { background: var(--accent-subtle); color: inherit; border-bottom: var(--comment-rail-default) solid var(--accent); }
	.thread { margin-top: var(--space-3); padding-left: var(--space-3); border-left: var(--comment-rail-default) solid var(--accent); }
	.text { margin: calc(var(--space-1) + var(--space-px-2)) 0 calc(var(--space-3) + var(--space-px-2)); } .thread > :last-child .text { margin-bottom: 0; }
	.text :global(p) { margin: 0 0 var(--space-3); } .text :global(p:last-child) { margin-bottom: 0; }
	.agent { color: var(--accent-text); }
	:global(.activity.phone) .bar { padding-inline: var(--space-3); }
	:global(.activity.phone) .scroll { padding-top: var(--space-4); padding-inline: var(--space-3); font-size: var(--text-input); }
</style>
