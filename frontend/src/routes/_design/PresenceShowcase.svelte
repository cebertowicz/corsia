<script lang="ts">
	import MarkdownEditor from '$lib/components/MarkdownEditor.svelte';
	import AvatarStack from '$lib/components/ui/AvatarStack.svelte';
	import type { PresenceRange } from '$lib/cm/presence-marks';

	const text = '# Launch notes\n\nThe release candidate is ready for review.\n';
	const cursor: PresenceRange[] = [{
		threadId: 'documents-agent',
		name: 'Documents',
		color: '#8b5cf6',
		span: { from: 17, to: 38 },
	}];
	const arrival: PresenceRange[] = [{
		threadId: 'documents-agent',
		name: 'Documents',
		color: '#8b5cf6',
		span: { from: 39, to: 55 },
		highlightSpan: { from: 17, to: 55 },
	}];
	const participants = [
		{ id: 'you', name: 'You', color: 'var(--text-tertiary)' },
		{ id: 'documents-agent', name: 'Documents', color: '#8b5cf6', detail: 'edited 20 s ago' },
	];
	const arrivalParticipants = participants.map((participant) => (
		participant.id === 'documents-agent' ? { ...participant, active: true } : participant
	));
</script>

<section class="block" id="document-presence">
	<header class="block-head">
		<h2>Recent document activity</h2>
		<p>Stable participant identity, the latest cursor, and a queued arrival highlight.</p>
	</header>
	<div class="presence-grid">
		<article class="card" data-presence-state="cursor">
			<div class="state-head"><span>Cursor</span><AvatarStack people={participants} /></div>
			<div class="editor"><MarkdownEditor value={text} readOnly presence={cursor} /></div>
		</article>
		<article class="card" data-presence-state="arrival">
			<div class="state-head"><span>Arrival highlight</span><AvatarStack people={arrivalParticipants} /></div>
			<div class="editor"><MarkdownEditor value={text} readOnly presence={arrival} /></div>
		</article>
	</div>
</section>

<style>
	.block { margin-bottom: 32px; }
	.block-head { margin-bottom: 12px; }
	.block-head h2 { margin: 0 0 4px; color: var(--text-primary); font-size: var(--text-body); font-weight: var(--fw-semibold); }
	.block-head p { margin: 0; color: var(--text-tertiary); font-size: var(--text-subhead); line-height: var(--lh-relaxed); }
	.presence-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
	.card { min-width: 0; padding: 16px; border: 1px solid var(--border-subtle); border-radius: var(--radius); background: var(--bg-secondary); }
	.state-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 14px; color: var(--text-secondary); font-size: var(--text-footnote); font-weight: var(--fw-semibold); }
	.editor { height: 150px; overflow: hidden; border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--bg-primary); }
	@media (max-width: 680px) { .presence-grid { grid-template-columns: 1fr; } }
</style>
