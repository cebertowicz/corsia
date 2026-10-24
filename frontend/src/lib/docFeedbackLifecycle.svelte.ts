import { untrack } from 'svelte';
import { DocFeedback } from '$lib/docFeedback.svelte';

/** Keep one document feedback batch while an open tab remains the same target. */
export function holdDocFeedback(
	of: () => { artifactId: string | null; targetInstanceId: string | null },
	onReturn?: () => void | Promise<void>,
): DocFeedback {
	const feedback = new DocFeedback(onReturn);
	$effect(() => {
		const { artifactId, targetInstanceId } = of();
		untrack(() => feedback.show(artifactId, targetInstanceId));
	});
	$effect(() => {
		const visibility = () => {
			if (document.visibilityState === 'visible') onReturn?.();
		};
		window.addEventListener('visibilitychange', visibility);
		return () => window.removeEventListener('visibilitychange', visibility);
	});
	$effect(() => {
		const leave = () => void feedback.leaveImage(undefined, true);
		window.addEventListener('pagehide', leave);
		return () => window.removeEventListener('pagehide', leave);
	});
	return feedback;
}

/** Send a viewer's image lane whenever its target leaves the composed surface. */
export function leaveImageOnExit(
	of: () => { feedback: DocFeedback | null | undefined; artifactId: string | null | undefined; key: string },
): void {
	// Each part is its own derived, so the exit runs only when the batch, the document or the tab really changes.
	const feedback = $derived(of().feedback);
	const artifactId = $derived(of().artifactId);
	const key = $derived(of().key);
	$effect(() => {
		const leaving = feedback;
		const id = artifactId;
		void key;
		return () => {
			if (leaving && id) void leaving.leaveImage(id);
		};
	});
}
