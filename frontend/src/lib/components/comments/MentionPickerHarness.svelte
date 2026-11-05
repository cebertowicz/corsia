<script lang="ts">
	import MentionPicker from './MentionPicker.svelte';
	import { provideMentionRoster } from './mentionContext';
	import type { DocumentReviewer } from '$lib/api/docReviews';
	import { createMentionSelection } from './mentionSelection';
	let { agents, status = 'ready', rosterError = false, retry = async () => {}, selectedTags = createMentionSelection(), body = '' }: { agents: DocumentReviewer[]; status?: 'loading' | 'ready' | 'error'; rosterError?: boolean; retry?: () => Promise<void>; selectedTags?: ReturnType<typeof createMentionSelection>; body?: string } = $props();
	provideMentionRoster({ get reviewers() { return agents; }, get loadState() { return status; }, get rosterError() { return rosterError; }, load: () => retry() });
	let input = $state<HTMLTextAreaElement>();
	let picker = $state<MentionPicker>();
	export function taggedAgentIds() { return picker?.taggedAgentIds(input?.value ?? '') ?? []; }
</script>
<textarea bind:this={input} aria-label="Comment" value={body}></textarea><MentionPicker bind:this={picker} {input} {selectedTags} />
