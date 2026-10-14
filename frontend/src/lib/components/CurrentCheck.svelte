<script lang="ts">
	/**
	 * The picker's one "current" mark, drawn on the current model row and the current account row.
	 * It sits in the row's left gutter beside the status dot or rollover order, at one inset for
	 * both rows, so the two checks share a column and no row's content moves for it. The row is
	 * its positioning context (`position: relative`) and may set `--check-top` to the centre of the
	 * line the check belongs beside; without it the check centres on the row.
	 */
</script>

<svg class="check-glyph" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" role="img" aria-label="Current"><path d="M20 6L9 17l-5-5"/></svg>

<style>
	.check-glyph { position: absolute; left: 10px; top: var(--check-top, 50%); transform: translateY(-50%); color: var(--violet); }
</style>
