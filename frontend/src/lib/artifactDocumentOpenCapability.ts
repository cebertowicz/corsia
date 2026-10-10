import type { ThreadArtifactWorkspace } from './artifactTabTypes';
import type { SplitLayout } from './artifactSplitLayout';

export interface DocumentOpenCapability {
	showing: string | null;
	canShow: boolean;
}

/**
 * Report whether an agent request can occupy the artifact leaf without
 * replacing anything currently on screen. `compact` is measured separately
 * because an empty compact canvas has the chat-only layout shape.
 */
export function documentOpenCapability(
	workspace: ThreadArtifactWorkspace,
	layout: SplitLayout,
	compact: boolean,
): DocumentOpenCapability {
	const activeTab = workspace.tabs.find((tab) => tab.instanceId === workspace.activeTabId) ?? null;
	const showing = activeTab?.kind === 'document' ? activeTab.artifactId : null;
	const occupied = activeTab !== null && (
		compact ? workspace.activeSurface === 'artifact' : layout.mode === 'split'
	);
	return { showing: occupied ? showing : null, canShow: !compact && !occupied };
}

/** What a canvas that shows nothing publishes, so a thread that stops displaying a document claims none. */
export const NO_DOCUMENT_OPEN: DocumentOpenCapability = { showing: null, canShow: false };
