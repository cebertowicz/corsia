<script lang="ts">
	let { open, onSelect, onCloseSheet, onClosed }: {
		open: boolean;
		onSelect: (instanceId: string) => void;
		onCloseSheet: () => void;
		onClosed: () => void;
	} = $props();
</script>

<button type="button" data-sheet-pick onclick={() => { onSelect('tab-2'); onCloseSheet(); }}>Pick</button>
<button type="button" data-sheet-finish-close onclick={onClosed}>Finish close</button>
<div data-sheet-open={open}></div>
