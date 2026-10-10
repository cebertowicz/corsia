<!-- Test-only fixture: isolates planQuery for direct primitive-level assertions. -->
<script lang="ts">
	import { planQuery } from '$lib/stores/planQuery.svelte';
	import type { OperatorKey } from '$lib/stores/planKeys';

	let { getKey }: { getKey: () => OperatorKey | null } = $props();
	const query = planQuery(() => getKey());
</script>

<div data-testid="q-status">{query.status}</div>
<div data-testid="q-token">{query.payload?.revision_token ?? ''}</div>
<button data-testid="q-retry" onclick={() => query.retry()}>Retry</button>
