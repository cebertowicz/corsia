<script lang="ts">
	import { tick } from 'svelte';
	import { get } from 'svelte/store';
	import { prefersReducedMotion } from '$lib/stores/viewport';
	import { captureMentionLayout, mentionHost, playMentionLayout, type MentionPlayback } from '$lib/actions/layoutCrossfade';
	import { clipFade } from '$lib/actions/clipFade';
	import AgentOptions from '../reviews/AgentOptions.svelte';
	import { agentOptionIndex } from '../reviews/agentOptionKeys';
	import { hasMentionRoster, useMentionRoster } from './mentionContext';
	import { createMentionSelection } from './mentionSelection';
	export function taggedAgentIds(text: string) { return selectedTags.ids(text); }
	export function occurrences(text: string) { return selectedTags.occurrences(text); }
	import type { DocumentReviewer } from '$lib/api/docReviews';
	let { input, selectedTags = createMentionSelection() }: { input: HTMLTextAreaElement | undefined; selectedTags?: ReturnType<typeof createMentionSelection> } = $props();
	const available = hasMentionRoster();
	const roster = useMentionRoster();
	const uniqueId = $props.id();
	const id = `mention-${uniqueId}`;
	let query = $state<string | null>(null), start = 0, active = $state(0);
	let lastQuery = '';
	let region = $state<HTMLElement>();
	let motionKey = '', motionGeneration = 0;
	let playback: MentionPlayback | undefined;
	function transition(node: HTMLElement) {
		return get(prefersReducedMotion) ? { duration: 0 } : clipFade(node);
	}
	let chosen: { start: number; text: string } | null = null;
	const matches = $derived(roster.loadState !== 'ready' || roster.rosterError ? [] : roster.reviewers.filter((agent) => agent.name.toLowerCase().includes((query ?? lastQuery).toLowerCase())));
	const status = $derived(roster.loadState === 'loading' ? 'loading' : roster.loadState === 'error' || roster.rosterError ? 'error' : !roster.reviewers.length ? 'empty' : 'ready');
	function refresh() {
		if (input) selectedTags.update(input.value);
		if (!available || !input || input.selectionStart !== input.selectionEnd) { query = null; return; }
		const before = input.value.slice(0, input.selectionStart), match = /(?:^|\s)@([^@\n]*)$/.exec(before);
		if (!match) { query = null; return; }
		start = before.length - match[1].length - 1;
		if (chosen && start === chosen.start && before.slice(start).startsWith(chosen.text)) { query = null; return; }
		lastQuery = match[1]; query = match[1]; active = 0;
	}
	async function choose(agent: DocumentReviewer) {
		if (!input || query === null) return;
		const end = input.selectionStart, next = `${input.value.slice(0, start)}@${agent.name} ${input.value.slice(end)}`;
		const pos = start + agent.name.length + 2; chosen = { start, text: `@${agent.name} ` };
		selectedTags.begin(start, end, 'insertText'); input.value = next;
		input.dispatchEvent(new Event('input', { bubbles:true }));
		selectedTags.select(agent.id, start, `@${agent.name}`); query = null;
		await tick(); input?.focus(); input?.setSelectionRange(pos, pos);
	}
	function preserveCaret(event: PointerEvent) {
		if (document.activeElement === input && (event.target as Element).closest('button')) event.preventDefault();
	}
	function keys(event: KeyboardEvent) {
		if (query === null) return;
		if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); query = null; return; }
		if (event.key === 'Enter' && matches.length) { event.preventDefault(); event.stopImmediatePropagation(); void choose(matches[Math.min(active, matches.length - 1)]); return; }
		const next = agentOptionIndex(event.key, active, matches.length);
		if (next !== null) { event.preventDefault(); event.stopImmediatePropagation(); active = next; void tick().then(() => document.getElementById(`${id}-option-${matches[active]?.id}`)?.scrollIntoView({ block:'nearest' })); }
	}
	$effect(() => {
		const node = input; if (!node) return;
		if (query !== null) {
			node.setAttribute('aria-autocomplete', 'list');
			node.setAttribute('aria-controls', id);
			const selected = matches[Math.min(active, matches.length - 1)];
			if (selected) node.setAttribute('aria-activedescendant', `${id}-option-${selected.id}`);
			else node.removeAttribute('aria-activedescendant');
		} else {
			node.removeAttribute('aria-autocomplete'); node.removeAttribute('aria-controls'); node.removeAttribute('aria-activedescendant');
		}
		return () => { node.removeAttribute('aria-autocomplete'); node.removeAttribute('aria-controls'); node.removeAttribute('aria-activedescendant'); };
	});
	$effect.pre(() => {
		const node = input;
		const key = query === null ? 'closed' : `${status}:${matches.map((agent) => `${agent.id}:${agent.name}:${agent.responsibility}`).join('|')}`;
		const previous = motionKey; motionKey = key;
		if (!node || !previous || key === previous) return;
		const reduced = get(prefersReducedMotion);
		if (!reduced && (previous === 'closed' || key === 'closed')) return;
		// The copy made to open the picker shows the layout from before the person saw any of it, so it is never handed on:
		// a change that lands while that fade is still running captures the layout actually on screen instead.
		const opening = previous === 'closed';
		let capture = playback?.handoff();
		if (!capture) {
			// Nothing carried, so the old playback must still be stopped: its copy would keep fading underneath the
			// new one and the host would keep its stale opacity animation. Then start the new fade at the opacities
			// the screen was showing, not at full opacity, which would step the picture up and back down.
			const seen = playback?.supersede();
			capture = captureMentionLayout(region ?? null, node, reduced);
			if (capture && seen?.midFade) Object.assign(capture, {from: seen.from, liveFrom: seen.liveFrom});
		}
		playback = undefined;
		if (!capture) return;
		const generation = ++motionGeneration;
		void tick().then(() => {
			if (generation !== motionGeneration || !node.isConnected) { capture.overlay.remove(); return; }
			const live = reduced ? mentionHost(node) : region ?? null;
			playback = playMentionLayout(capture, live, reduced, !opening);
		});
	});
	$effect(() => {
		const node = input; if (!node) return;
		const cancel = () => { motionGeneration++; playback?.cancel(); playback = undefined; };
		const beforeInput = (event: InputEvent) => selectedTags.begin(node.selectionStart, node.selectionEnd, event.inputType);
		node.addEventListener('beforeinput', beforeInput);
		node.addEventListener('input', cancel); node.addEventListener('compositionstart', cancel);
		node.addEventListener('input', refresh); node.addEventListener('click', refresh); node.addEventListener('keydown', keys, true);
		return () => { cancel(); node.removeEventListener('beforeinput', beforeInput); node.removeEventListener('input', cancel); node.removeEventListener('compositionstart', cancel); node.removeEventListener('input', refresh); node.removeEventListener('click', refresh); node.removeEventListener('keydown', keys, true); };
	});
</script>
{#if query !== null}
	<!-- The children provide the keyboard controls; these handlers preserve the textarea and contain pointer clicks. -->
	<!-- svelte-ignore a11y_no_static_element_interactions, a11y_click_events_have_key_events -->
	<div class="mention-region" bind:this={region} transition:transition onpointerdown={preserveCaret} onclick={(event) => event.stopPropagation()}><AgentOptions {id} label="Agents to tag" {matches} active={Math.min(active, matches.length - 1)} {choose} {status} retry={roster.load} /></div>
{/if}
<style>
	.mention-region { padding-top: var(--space-2); }
</style>
