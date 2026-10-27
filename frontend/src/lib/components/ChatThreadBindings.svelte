<script lang="ts">
	import { onDestroy } from 'svelte';
	import { activeThreadId, threads } from '$lib/stores/threads';
	import { bindActiveThreadModel, threadModelVersion } from '$lib/stores/models';
	import { loadCheckpoints } from '$lib/stores/checkpoints';
	import { token } from '$lib/stores/auth';
	import { startPresence, stopPresence } from '$lib/ws/presence';

	// The main panel is the sole view that reports its displayed thread.
	$effect(() => {
		const threadId = $activeThreadId;
		if (threadId) startPresence(threadId);
		else stopPresence();
	});
	onDestroy(stopPresence);

	// Every navigation path binds the composer to the opened thread's saved model.
	$effect(() => {
		const id = $activeThreadId;
		void $threadModelVersion; // a worker's model arrives as a read after the thread opens
		if (id) bindActiveThreadModel(id, $threads);
	});
	$effect(() => {
		if ($activeThreadId && $token) void loadCheckpoints($activeThreadId, $token).catch(() => null);
	});
</script>
