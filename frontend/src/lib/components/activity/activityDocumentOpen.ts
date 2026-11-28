import { get } from 'svelte/store';
import { requestArtifactTabClose } from '$lib/artifactTabClose';
import { openArtifactTab, openBrowserPane, threadWorkspaces } from '$lib/stores/artifactWorkspace';
import { findDocument, type DocumentLookup } from './activityItems';

/** Replace the previous Activity tab through the ordinary close path. */
async function closeAllBut(workspaceKey: string, keep: string | null) {
	const tabs = get(threadWorkspaces)[workspaceKey]?.tabs ?? [];
	for (const tab of tabs) {
		if (tab.instanceId !== keep) await requestArtifactTabClose(workspaceKey, tab, () => {});
	}
}

export function openActivityBrowser(workspaceKey: string, threadId: string): void {
	openBrowserPane(workspaceKey, false, threadId);
	const open = get(threadWorkspaces)[workspaceKey]?.activeTabId ?? null;
	void closeAllBut(workspaceKey, open);
}

/** Resolve the document and replace the previous Activity tab through the ordinary close path. */
export function openActivityDocument(
	workspaceKey: string, artifactId: string | null, thread: string | null,
	landOnReplies: boolean, commentId: string | undefined,
	setLookup: (value: DocumentLookup | { status: 'resolving' } | null) => void,
	path?: string,
): () => void {
	if (!artifactId && path && thread) {
		const title = path.split('/').pop() ?? path;
		setLookup({ status: 'found', path, title });
		openArtifactTab(workspaceKey, { path, title });
		void closeAllBut(workspaceKey, get(threadWorkspaces)[workspaceKey]?.activeTabId ?? null);
		return () => {};
	}
	if (!artifactId) {
		setLookup(null);
		void closeAllBut(workspaceKey, null);
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
		void closeAllBut(workspaceKey, open);
	});
	return () => { current = false; };
}
