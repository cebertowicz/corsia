<script lang="ts">
	// Stands in for ChatPanel only: one real spawn chip inside the chat column, as a
	// durable spawn-marker row renders it. SubagentChip itself is the real component
	// and the click keeps bubbling through the real chat-column ancestor.
	import SubagentChip from '$lib/components/SubagentChip.svelte';

	// The spawn-marker message as the transcript holds it: its metadata is durable,
	// so the chip knows its worker with no live map and no network read.
	const msg = {
		id: 'm1',
		metadata: {
			child_thread_id: 'child-1',
			parent_thread_id: 't1',
			thread_kind: 'subagent',
			name: 'Review worker',
			provider: 'cc',
		},
	};
</script>

<div class="messages"><SubagentChip {msg} /></div>
