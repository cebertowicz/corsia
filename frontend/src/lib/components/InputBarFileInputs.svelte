<script lang="ts">
	let { onFiles }: { onFiles: (files: FileList | null) => void } = $props();

	let fileInputEl = $state<HTMLInputElement | undefined>(undefined);
	let cameraInputEl = $state<HTMLInputElement | undefined>(undefined);
	let photosInputEl = $state<HTMLInputElement | undefined>(undefined);

	export function openFile() {
		fileInputEl?.click();
	}

	export function openCamera() {
		cameraInputEl?.click();
	}

	export function openPhotos() {
		photosInputEl?.click();
	}
</script>

<input
	bind:this={fileInputEl}
	type="file"
	multiple
	data-composer-attachment-input
	accept="*/*"
	tabindex="-1"
	style="display: none"
	onchange={(e: Event) => onFiles((e.target as HTMLInputElement).files)}
/>
<input
	bind:this={cameraInputEl}
	type="file"
	accept="image/*"
	capture="environment"
	tabindex="-1"
	style="display: none"
	onchange={(e: Event) => onFiles((e.target as HTMLInputElement).files)}
/>
<input
	bind:this={photosInputEl}
	type="file"
	multiple
	accept="image/jpeg,image/png,image/heic,image/heif,image/webp"
	tabindex="-1"
	style="display: none"
	onchange={(e: Event) => onFiles((e.target as HTMLInputElement).files)}
/>
