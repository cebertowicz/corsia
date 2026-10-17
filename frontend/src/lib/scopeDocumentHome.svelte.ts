import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';
import { canOpenArtifact } from '$lib/openArtifactInThread';
import { fetchScopeHomeThread } from '$lib/utils/conductor';

/**
 * Where a document of one scope opens when Settings has no thread to open it beside (Settings opened from
 * Activity): the scope's own thread, the project's conductor or the workspace's CORSIA agent. Looked up
 * only when nothing else can own the tab. Call during component setup.
 *
 * `canOpen` is what a row asks to be a button; `unavailable` is true once the lookup has finished and
 * found no thread, so the row stays text and the page can say why.
 */
export function createScopeDocumentHome(projectId: () => string | null, namedThread: () => string | null = () => null) {
	let thread = $state<string | null>(null);
	let settled = $state(false);

	$effect(() => {
		const project = projectId();
		if (canOpenArtifact(namedThread())) {
			thread = null;
			settled = true;
			return;
		}
		settled = false;
		let current = true;
		void fetchScopeHomeThread(get(token), project).then((found) => {
			if (!current) return;
			thread = found;
			settled = true;
		});
		return () => { current = false; };
	});

	return {
		get thread() { return thread; },
		get canOpen() { return canOpenArtifact(namedThread(), thread); },
		get unavailable() { return settled && !canOpenArtifact(namedThread(), thread); },
	};
}
