import { writable, get } from 'svelte/store';
import { registerArtifact } from '$lib/api/docs';
import { resolveArtifactTab } from '$lib/stores/artifactWorkspace';

/**
 * A projection of the document currently mounted in `DocViewer`.
 *
 * This is the single channel the comments pane reads from — it replaces the
 * former `openDocKey` / `openDocTitle` / `openDocText` trio, which were three
 * independent writables that could disagree about which document was on screen.
 * It is a *projection*, not a source of truth: the workspace owns which tab is
 * open, and the mounted viewer owns what it has actually loaded.
 */
export interface ActiveDocEditor {
	/** Tab instance that owns this editor session. */
	instanceId: string;
	artifactId: string | null;
	path: string;
	projectId: string | null;
	threadId: string | null;
	title: string;
	/** Text currently in the editor — what comment anchors resolve against. */
	text: string;
	readonly: boolean;
}

export const activeDocEditor = writable<ActiveDocEditor | null>(null);

/**
 * Resolves the artifact id needed to attach a comment, registering the file if
 * it has never been registered.
 *
 * The resolved id is propagated to the owning tab as well, so the tab, its
 * comments, and its change baseline never drift onto different identities.
 */
export async function ensureEditorArtifactId(): Promise<string | null> {
	const editor = get(activeDocEditor);
	if (!editor) return null;
	if (editor.artifactId) return editor.artifactId;
	const row = await registerArtifact(editor.path, {
		projectId: editor.projectId,
		threadId: editor.threadId,
	});
	activeDocEditor.update((cur) =>
		cur && cur.instanceId === editor.instanceId ? { ...cur, artifactId: row.id } : cur,
	);
	if (editor.threadId) resolveArtifactTab(editor.threadId, editor.instanceId, row.id);
	return row.id;
}

/** Clears the projection only if this editor session still owns it. */
export function releaseDocEditor(instanceId: string): void {
	activeDocEditor.update((cur) => (cur && cur.instanceId === instanceId ? null : cur));
}
