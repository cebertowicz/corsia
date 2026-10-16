import type { Thread } from '$lib/stores/threads';

export type View = 'activity' | 'chat' | 'settings' | 'documents' | 'measurement';
export type Section = {
	title: string;
	meta?: string;
	empty?: string;
	kind?: 'projects' | 'plan' | 'artifacts';
	initialOpen?: boolean;
};
export type PaneConfig = { label: string; sections: Section[] };
type Scope = { id: string; name: string; color?: string } | null;

const WORKSPACE_SECTIONS: Section[] = [
	{ title: 'Projects', empty: 'No active projects', kind: 'projects' },
];

const SCOPE_SECTIONS: Section[] = [
	{ title: 'Roadmap', empty: 'No roadmap yet' },
	{ title: 'Documents', empty: 'No documents yet' },
];

const CHAT_SECTIONS: Section[] = [
	{ title: 'Plan', kind: 'plan' },
	{ title: 'Artifacts', kind: 'artifacts' },
];

/**
 * The right pane's content for the current view, or null when the view has no pane.
 * Only workspace-scope Activity lists Projects; other non-chat views and the settings
 * takeovers (from any view) carry no pane.
 * Settings live in the header's one gear, never in the pane.
 */
export function buildPaneConfig(
	activeView: View,
	activeThread: Thread | null,
	scope: Scope,
	activeThreadId?: string | null,
	settingsTakeover = false,
): PaneConfig | null {
	// Workspace/Project settings are a takeover screen: no pane, wherever they were opened from.
	if (settingsTakeover) return null;
	if (activeView === 'chat') {
		// Resolve the active thread ID from the full Thread object when available,
		// falling back to activeThreadId for the window where $activeThreadId is set
		// but the thread object has not loaded into $threads yet.
		const resolvedId = activeThread?.id ?? activeThreadId ?? null;
		const label = scope?.name ?? 'Workspace';
		// The pane keys off the thread id, so a thread missing from the store (hidden subagent,
		// or the window before the list loads) still gets its Plan and Artifacts.
		if (activeThread || resolvedId) return { label, sections: CHAT_SECTIONS };
		if (scope) return { label: scope.name, sections: SCOPE_SECTIONS };
		// No thread and no scope (the new-chat landing): nothing to list.
		return null;
	}
	if (activeView === 'activity' && !scope) {
		return { label: 'Workspace', sections: WORKSPACE_SECTIONS };
	}
	return null;
}
