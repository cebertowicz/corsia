<script lang="ts">
	let { reference }: { reference: string } = $props();
</script>

<p class="reference">Reference: {reference}</p>

<style>
	.reference { margin: var(--space-1) 0 0; font-size: var(--text-subhead); color: var(--text-secondary); }
</style>
