import { get } from 'svelte/store';
import { requestArtifactTabClose } from '$lib/artifactTabClose';
import { openArtifactTab, threadWorkspaces } from '$lib/stores/artifactWorkspace';
import { findDocument, type DocumentLookup } from './activityItems';

/** Resolve the document and replace the previous Activity tab through the ordinary close path. */
export function openActivityDocument(
	workspaceKey: string, artifactId: string | null, thread: string | null,
	landOnReplies: boolean, commentId: string | undefined,
	setLookup: (value: DocumentLookup | { status: 'resolving' } | null) => void,
	path?: string,
): () => void {
	async function closeAllBut(keep: string | null) {
		const tabs = get(threadWorkspaces)[workspaceKey]?.tabs ?? [];
		for (const tab of tabs) {
			if (tab.instanceId !== keep) await requestArtifactTabClose(workspaceKey, tab, () => {});
		}
	}
	if (!artifactId && path && thread) {
		const title = path.split('/').pop() ?? path;
		setLookup({ status: 'found', path, title });
		openArtifactTab(workspaceKey, { path, title });
		void closeAllBut(get(threadWorkspaces)[workspaceKey]?.activeTabId ?? null);
		return () => {};
	}
	if (!artifactId) {
		setLookup(null);
		void closeAllBut(null);
		return () => {};
	}
	if (!thread) {
		setLookup({ status: 'missing' });
		return () => {};
	}
	setLookup({ status: 'resolving' });
	let current = true;
	void findDocument(artifactId).then((found) => {
		if (!current) return;
		setLookup(found);
		if (found.status !== 'found') return;
		openArtifactTab(workspaceKey, {
			artifactId,
			path: found.path,
			title: found.title ?? found.path.split('/').pop() ?? found.path,
			...(landOnReplies ? { landOn: 'new-replies' as const, landOnCommentId: commentId } : {}),
		});
		const open = get(threadWorkspaces)[workspaceKey]?.activeTabId ?? null;
		void closeAllBut(open);
	});
	return () => { current = false; };
}
