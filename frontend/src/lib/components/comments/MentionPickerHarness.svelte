<script lang="ts">
	import MentionPicker from './MentionPicker.svelte';
	import { provideMentionRoster } from './mentionContext';
	import type { DocumentReviewer } from '$lib/api/docReviews';
	let { agents, status = 'ready', rosterError = false, retry = async () => {} }: { agents: DocumentReviewer[]; status?: 'loading' | 'ready' | 'error'; rosterError?: boolean; retry?: () => Promise<void> } = $props();
	provideMentionRoster({ get reviewers() { return agents; }, get loadState() { return status; }, get rosterError() { return rosterError; }, load: () => retry() });
	let input = $state<HTMLTextAreaElement>();
	let picker = $state<MentionPicker>();
	export function taggedAgentIds() { return picker?.taggedAgentIds(input?.value ?? '') ?? []; }
</script>
<textarea bind:this={input} aria-label="Comment"></textarea><MentionPicker bind:this={picker} {input} />
