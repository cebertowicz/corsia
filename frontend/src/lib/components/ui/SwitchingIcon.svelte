<script lang="ts">
	interface Props {
		/** Accessible switch state, such as "Switching account". */
		ariaLabel: string;
		/** Diameter in px. */
		size?: number;
	}

	const { ariaLabel, size = 12 }: Props = $props();
</script>

<svg
	class="switching-icon"
	width={size}
	height={size}
	viewBox="0 0 24 24"
	fill="none"
	stroke="currentColor"
	stroke-width="2"
	stroke-linecap="round"
	stroke-linejoin="round"
	role="img"
	aria-label={ariaLabel}
>
	<path d="M7 7.5A7 7 0 0 1 18.5 9" />
	<path d="m18.5 5.5 0 3.5-3.5 0" />
	<path d="M17 16.5A7 7 0 0 1 5.5 15" />
	<path d="m5.5 18.5 0-3.5 3.5 0" />
</svg>

<style>
	.switching-icon {
		display: inline-block;
		flex: 0 0 auto;
		color: var(--accent);
		vertical-align: -0.1em;
		animation: switching-icon-spin 1s linear infinite;
	}

	@keyframes switching-icon-spin {
		to { transform: rotate(360deg); }
	}
</style>
