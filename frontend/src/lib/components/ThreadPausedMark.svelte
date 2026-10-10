<script lang="ts">
	// The row-leading pause mark: a paused plan sits at the left of the name,
	// in the slot the rail's agent sparkle owns and the chat row reserves
	// ahead of its title. Gray tertiary, quiet, static — motion belongs to
	// turn truth, and a pause is the absence of it.
	let { size = 16 }: { size?: number } = $props();
</script>

<svg
	class="paused-mark"
	width={size}
	height={size}
	viewBox="0 0 24 24"
	role="img"
	aria-label="Paused"
>
	<title>Paused</title>
	<!-- Fill is the token itself, not currentColor: rail context styles any
	     child svg (including this one) with the accent color. -->
	<rect x="7" y="5" width="3.6" height="14" rx="1.5" fill="var(--text-tertiary)" />
	<rect x="13.4" y="5" width="3.6" height="14" rx="1.5" fill="var(--text-tertiary)" />
</svg>

<style>
	.paused-mark { flex-shrink: 0; }
</style>
