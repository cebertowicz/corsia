<script lang="ts">
	import { openWaitArtifact } from '$lib/openWaitArtifact';
	import type { WaitFace } from './waitFace';

	// One line at every width, never wrapping (EW8): only the label truncates, and the state word and the time always show.
	// Below 480 px the elapsed time goes before the label shrinks; the title and accessible name keep it.
	// `decorative`: the toggle beside it already names the whole face, so the plain text is hidden from assistive
	// technology; the label link stays its own control.
	let { face, threadId = null, decorative = false }: { face: WaitFace; threadId?: string | null; decorative?: boolean } = $props();

	// The label links only when there is a thread to open the document from. A document that fails to open stops
	// being a link, and its title says so.
	let unopenable = $state<string | null>(null);
	const linked = $derived(face.artifactId && threadId && unopenable !== face.artifactId ? face.artifactId : null);

	async function open(): Promise<void> {
		if (!linked || !threadId) return;
		const artifactId = linked;
		if (!(await openWaitArtifact(artifactId, face.label, threadId))) unopenable = artifactId;
	}
</script>

<span class="wait-face" title={face.title}>
	<span class="wait-fixed" aria-hidden={decorative || undefined}>Waiting ·</span>
	{#if face.elapsed}<span class="wait-fixed wait-elapsed" aria-hidden={decorative || undefined}>{face.elapsed} ·</span>{/if}
	{#if linked}
		<button type="button" class="wait-label wait-link" onclick={open}>{face.label}</button>
	{:else if face.artifactId && unopenable === face.artifactId}
		<span class="wait-label" title="Couldn't open this document">{face.label}</span>
	{:else}
		<span class="wait-label" aria-hidden={decorative || undefined}>{face.label}</span>
	{/if}
	<span class="wait-fixed" aria-hidden={decorative || undefined}>· {face.back}</span>
</span>

<style>
	.wait-face { display: flex; flex-wrap: nowrap; column-gap: 0.3em; min-width: 0; overflow: hidden; white-space: nowrap; }
	.wait-fixed { flex: 0 0 auto; white-space: nowrap; }
	.wait-label { flex: 0 1 auto; min-width: 10ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.wait-link { padding: 0; border: 0; background: transparent; color: inherit; font: inherit; text-align: left; text-decoration: underline; cursor: pointer; }
	.wait-link:hover { color: var(--text-primary); }
	.wait-link:focus-visible { outline: 2px solid var(--accent); outline-offset: -1px; border-radius: var(--radius-xs); }
	@media (max-width: 30rem) {
		.wait-elapsed { display: none; }
	}
</style>
