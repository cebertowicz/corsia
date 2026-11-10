<script lang="ts">
	import { tick } from 'svelte';
	import { useLayoutCrossfade } from './layoutCrossfade.svelte';
	import { settleHeight } from '$lib/actions/settleHeight';
	import { prefersReducedMotion } from '$lib/stores/viewport';
	import AgentOptions from '../reviews/AgentOptions.svelte';
	import { agentOptionIndex } from '../reviews/agentOptionKeys';
	import { hasMentionRoster, useMentionRoster } from './mentionContext';
	import { createMentionSelection, withTypedMentions } from './mentionSelection';
	export function taggedAgentIds(text: string) { return [...new Set(occurrences(text).map(item => item.agent_id))]; }
	export function occurrences(text: string) {
		const picked = selectedTags.occurrences(text);
		// Tags need the chat the document was opened from, so typed names resolve only where picking could.
		return withTypedMentions(text, roster.noViewingThread ? [] : roster.taggableAgents, picked);
	}
	import type { TaggableAgent } from '$lib/api/docReviews';
	let { input, selectedTags = createMentionSelection() }: { input: HTMLTextAreaElement | undefined; selectedTags?: ReturnType<typeof createMentionSelection> } = $props();
	const available = hasMentionRoster();
	const roster = useMentionRoster();
	const uniqueId = $props.id();
	const id = `mention-${uniqueId}`;
	let query = $state<string | null>(null), start = 0, active = $state(0);
	let lastQuery = '';
	let chosen: { start: number; text: string } | null = null;
	const matches = $derived(roster.noViewingThread || roster.loadState !== 'ready' || roster.rosterError ? [] : roster.taggableAgents.filter((agent) => agent.name.toLowerCase().includes((query ?? lastQuery).toLowerCase())));
	const status = $derived(roster.noViewingThread ? 'no_thread' : roster.loadState === 'loading' ? 'loading' : roster.loadState === 'error' || roster.rosterError ? 'error' : !roster.taggableAgents.length ? 'empty' : 'ready');
	// What the picker shows: a change to it crossfades the panel around it under reduced motion.
	const shownKey = $derived(query === null ? 'closed' : `${status}:${matches.map((agent) => `${agent.id}:${agent.name}:${agent.responsibility}`).join('|')}`);
	const transition = useLayoutCrossfade(() => input, () => shownKey);
	function refresh() {
		if (input) selectedTags.update(input.value);
		if (!available || !input || input.selectionStart !== input.selectionEnd) { query = null; return; }
		const before = input.value.slice(0, input.selectionStart), match = /(?:^|\s)@([^@\n]*)$/.exec(before);
		if (!match) { query = null; return; }
		start = before.length - match[1].length - 1;
		if (chosen && start === chosen.start && before.slice(start).startsWith(chosen.text)) { query = null; return; }
		lastQuery = match[1]; query = match[1]; active = 0;
	}
	async function choose(agent: TaggableAgent) {
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
	$effect(() => {
		const node = input; if (!node) return;
		const beforeInput = (event: InputEvent) => selectedTags.begin(node.selectionStart, node.selectionEnd, event.inputType);
		node.addEventListener('beforeinput', beforeInput);
		node.addEventListener('input', refresh); node.addEventListener('click', refresh); node.addEventListener('keydown', keys, true);
		return () => { node.removeEventListener('beforeinput', beforeInput); node.removeEventListener('input', refresh); node.removeEventListener('click', refresh); node.removeEventListener('keydown', keys, true); };
	});
</script>
{#if query !== null}
	<!-- The children provide the keyboard controls; these handlers preserve the textarea and contain pointer clicks.
	     Filtering eases the list to its new height (the composer popover has no height ease of its own); reduced motion cuts and crossfades instead. -->
	<!-- svelte-ignore a11y_no_static_element_interactions, a11y_click_events_have_key_events -->
	<div class="mention-region" transition:transition use:settleHeight={{ enabled: !$prefersReducedMotion }} onpointerdown={preserveCaret} onclick={(event) => event.stopPropagation()}><AgentOptions {id} label="Agents to tag" {matches} active={Math.min(active, matches.length - 1)} {choose} {status} retry={roster.load} /></div>
{/if}
<style>
	.mention-region { padding-top: var(--space-2); overflow: clip; }
</style>
