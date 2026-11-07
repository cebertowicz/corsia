<script lang="ts">
	// Stands in for ArtifactLeaf's heavy body only, so the measurement stays on
	// ChatArtifactSplit's own rendering decision. `mode` is the real `leafMode` value
	// the split passes its leaf, so what is recorded is the product's branch
	// (rail / expanded / compact), never a fixture's choice.
	let { mode }: { mode: 'rail' | 'expanded' | 'compact' } = $props();
</script>

<div class="leaf-probe" data-leaf-mode={mode}></div>
