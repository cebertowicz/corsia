<script lang="ts">
	import { Button } from '$lib/components/ui';

	/** Back on the connect screens: to Choose a computer from the code, to where Connect was opened from otherwise. */
	const { onclick }: { onclick: () => void } = $props();
</script>

<Button variant="chip" {onclick}>
	<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 18l-6-6 6-6" /></svg>
	Back
</Button>
