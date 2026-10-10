import { activeDocEditor } from '$lib/stores/activeDocEditor';
import type { DocViewerTarget } from '$lib/docViewerTarget';

interface ActiveDocumentEditorState {
	target: DocViewerTarget | null;
	draft: unknown;
	docState: 'loading' | 'ready' | 'error';
	artifactId: string | null;
	text: string;
	readonly: boolean;
}

/** Keep the global editor projection aligned with the mounted document tab. */
export function syncActiveDocumentEditor(state: ActiveDocumentEditorState): void {
	void state.draft;
	const { target } = state;
	if (!target || state.docState === 'loading') return;
	activeDocEditor.set({
		instanceId: target.instanceId,
		artifactId: state.artifactId,
		path: target.path,
		projectId: target.projectId,
		threadId: target.threadId,
		title: target.name,
		text: state.text,
		readonly: state.readonly,
	});
}
