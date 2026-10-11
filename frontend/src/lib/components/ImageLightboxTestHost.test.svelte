<script lang="ts">
	import ImageLightbox from './ImageLightbox.svelte';

	let src = $state('/first.png');

	export function setSource(next: string) {
		src = next;
	}
</script>

<ImageLightbox {src} alt="Mock screen" />
