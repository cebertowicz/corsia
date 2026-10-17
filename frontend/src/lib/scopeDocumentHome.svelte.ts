import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';
import { canOpenArtifact } from '$lib/openArtifactInThread';
import { fetchScopeTarget, type CoordinationLookup } from '$lib/utils/conductor';

/**
 * Where a document of one scope opens when Settings has no thread to open it beside (Settings opened from
 * Activity): the scope's own thread, the project's conductor or the workspace's CORSIA agent. Looked up
 * only when nothing else can own the tab. Call during component setup.
 *
 * `canOpen` is what a row asks to be a button. `unavailable` is true once the lookup confirmed the scope
 * has no thread, so the row stays text and the page can say why. `failed` is true when the lookup itself
 * failed, which says nothing about whether the thread exists; `retry` asks again.
 */
export function createScopeDocumentHome(projectId: () => string | null, namedThread: () => string | null = () => null) {
	let lookup = $state<CoordinationLookup | null>(null);
	let attempt = $state(0);

	$effect(() => {
		const project = projectId();
		void attempt;
		lookup = null;
		if (canOpenArtifact(namedThread())) return;
		let current = true;
		const scope = project ? { id: project, name: '', color: '' } : null;
		void fetchScopeTarget(get(token), scope).then((found) => {
			if (current) lookup = found;
		});
		return () => { current = false; };
	});

	const thread = () => (lookup?.kind === 'target' ? lookup.target.thread_id : null);

	return {
		get thread() { return thread(); },
		get canOpen() { return canOpenArtifact(namedThread(), thread()); },
		get unavailable() { return lookup?.kind === 'absent' && !canOpenArtifact(namedThread()); },
		get failed() { return lookup?.kind === 'unknown' && !canOpenArtifact(namedThread()); },
		retry() { attempt += 1; },
	};
}
