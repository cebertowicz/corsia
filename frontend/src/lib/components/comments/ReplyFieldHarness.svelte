<script lang="ts">
	import ReplyField from './ReplyField.svelte';
	import { provideMentionRoster } from './mentionContext';
	import { createMentionSelection } from './mentionSelection';
	import type { Recipient } from './recipients';
	import type { TaggableAgent } from '$lib/api/docReviews';
	let { agents, goesTo, value = '', selectedTags = createMentionSelection(value) }: { agents: TaggableAgent[]; goesTo: Recipient[]; value?: string; selectedTags?: ReturnType<typeof createMentionSelection> } = $props();
	provideMentionRoster({ get taggableAgents() { return agents; }, loadState: 'ready', rosterError: false, noViewingThread: false, load: async () => {} });
	let text = $state(value);
</script>
<ReplyField bind:value={text} {selectedTags} {goesTo} onSend={() => {}} />
