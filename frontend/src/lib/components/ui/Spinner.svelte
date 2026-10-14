<script lang="ts">
	/** The in-progress ring: the mark the toast has always carried, as one component. Decorative; the text beside it says what is happening. */
	interface Props {
		/** Diameter in px. */
		size?: number;
	}

	const { size = 14 }: Props = $props();
</script>

<span class="spinner" style:width="{size}px" style:height="{size}px" aria-hidden="true"></span>

<style>
	.spinner {
		display: inline-block;
		flex-shrink: 0;
		box-sizing: border-box;
		border: 2px solid color-mix(in srgb, var(--accent) 25%, transparent);
		border-top-color: var(--accent);
		border-radius: var(--radius-circle);
		animation: spinner-spin 0.8s linear infinite;
	}

	@keyframes spinner-spin {
		to { transform: rotate(360deg); }
	}

	@media (prefers-reduced-motion: reduce) {
		.spinner { animation-duration: 2.4s; }
	}
</style>
