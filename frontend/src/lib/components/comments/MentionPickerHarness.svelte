<script lang="ts">
	import MentionPicker from './MentionPicker.svelte';
	import { provideMentionRoster } from './mentionContext';
	import type { TaggableAgent } from '$lib/api/docReviews';
	import { createMentionSelection } from './mentionSelection';
	let { agents, status = 'ready', rosterError = false, noViewingThread = false, retry = async () => {}, selectedTags = createMentionSelection(), body = '' }: { agents: TaggableAgent[]; status?: 'loading' | 'ready' | 'error'; rosterError?: boolean; noViewingThread?: boolean; retry?: () => Promise<void>; selectedTags?: ReturnType<typeof createMentionSelection>; body?: string } = $props();
	provideMentionRoster({ get taggableAgents() { return agents; }, get loadState() { return status; }, get rosterError() { return rosterError; }, get noViewingThread() { return noViewingThread; }, load: () => retry() });
	let input = $state<HTMLTextAreaElement>();
	let picker = $state<MentionPicker>();
	export function taggedAgentIds() { return [...new Set((picker?.occurrences(input?.value ?? '') ?? []).map(item => item.agent_id))]; }
</script>
<textarea bind:this={input} aria-label="Comment" value={body}></textarea><MentionPicker bind:this={picker} {input} {selectedTags} />
