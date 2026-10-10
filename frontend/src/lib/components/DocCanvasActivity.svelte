<script lang="ts">
	import { tick } from 'svelte';
	import { EditorView } from '@codemirror/view';
	import { Button } from './ui';
	import { displayedDocument } from '$lib/ws/presence';
	import { isViewing, onRecordedDocActivity, type DocActivityFrame, type DocSpan } from '$lib/docPresence';
	import { UnseenDocActivity, locateDocumentActivity, type ActivityPill } from '$lib/docCanvasActivity';

	let { root, artifactId, threadId, onJump }: {
		root?: HTMLElement; artifactId: string | null; threadId: string | null;
		onJump: (span: DocSpan) => void;
	} = $props();
	const unseen = new UnseenDocActivity();
	const eligible = new Set<string>();
	let pill = $state<ActivityPill | null>(null);
	let top = $state(0);
	let right = $state(8);
	let visibility = $state<DocumentVisibilityState>('visible');
	const viewing = $derived(isViewing(artifactId, threadId, $displayedDocument, visibility));
	let pendingMeasure: number | null = null;
	const identity = (frame: DocActivityFrame) => JSON.stringify([frame.artifact_id, frame.thread_id, frame.kind, frame.at, frame.comment_id, frame.revision]);
	function editor() {
		const element = root?.querySelector<HTMLElement>('.cm-content');
		return element ? EditorView.findFromDOM(element) : null;
	}
	function locate(from: number, to: number) {
		const view = editor(), clip = root?.querySelector('.doc-main')?.getBoundingClientRect();
		return view && clip ? locateDocumentActivity(view, clip, from, to) : null;
	}
	function measure() {
		pendingMeasure = null;
		if (!viewing) { unseen.clear(); eligible.clear(); pill = null; return; }
		unseen.seen(locate);
		pill = unseen.pill(locate);
		const clip = root?.querySelector('.doc-main')?.getBoundingClientRect();
		if (clip && root && pill) {
			const box = root.getBoundingClientRect();
			top = (pill.direction === 'above' ? clip.top + 4 : clip.bottom - 48) - box.top;
			right = box.right - clip.right + 8;
		}
	}
	function schedule() {
		if (pendingMeasure !== null) return;
		pendingMeasure = requestAnimationFrame(() => { void tick().then(measure); });
	}
	export function editorChanged(mapPos?: (offset: number) => number) {
		if (mapPos) unseen.map(mapPos);
		schedule();
	}
	export async function arrive(frame: DocActivityFrame) {
		if (!viewing || !eligible.delete(identity(frame))) return;
		await tick();
		if (!viewing) return;
		unseen.add(frame, locate);
		schedule();
	}
	$effect(() => {
		artifactId; threadId;
		unseen.clear(); eligible.clear(); pill = null;
	});
	$effect(() => {
		viewing;
		if (!viewing) { unseen.clear(); eligible.clear(); pill = null; }
		schedule();
	});
	$effect(() => {
		const element = root;
		if (!element) return;
		const recorded = onRecordedDocActivity((frame) => {
			if (frame.artifact_id !== artifactId || !viewing || !frame.span || frame.kind === 'decision') return;
			eligible.add(identity(frame));
			if (frame.kind === 'read') void arrive(frame);
		});
		const resize = new ResizeObserver(schedule);
		resize.observe(element);
		const updateVisibility = () => { visibility = document.visibilityState; };
		updateVisibility();
		document.addEventListener('visibilitychange', updateVisibility);
		element.addEventListener('scroll', schedule, true);
		return () => {
			recorded(); resize.disconnect();
			document.removeEventListener('visibilitychange', updateVisibility);
			element.removeEventListener('scroll', schedule, true);
			if (pendingMeasure !== null) cancelAnimationFrame(pendingMeasure);
			pendingMeasure = null; unseen.clear(); eligible.clear();
		};
	});
</script>

{#if viewing && pill}
	<div class="activity-pill" data-doc-activity-pill style:top="{top}px" style:right="{right}px">
		<Button size="sm" ariaLabel={`${pill.count} new ${pill.direction}, go to nearest`} onclick={() => { if (pill) onJump(pill.span); }}>
			{pill.count} new {pill.direction}
			<svg aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d={pill.direction === 'above' ? 'M12 19V5m-6 6 6-6 6 6' : 'M12 5v14m-6-6 6 6 6-6'} /></svg>
		</Button>
	</div>
{/if}

<style>
	.activity-pill { position: absolute; right: 8px; z-index: 8; border-radius: var(--radius-pill); background: var(--bg-primary); box-shadow: var(--shadow-chip); }
	.activity-pill :global(button) { min-height: 44px; border-radius: var(--radius-pill); }
</style>
