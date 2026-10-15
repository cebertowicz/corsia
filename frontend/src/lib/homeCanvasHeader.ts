import { canvasHeader } from '$lib/stores/canvasHeader';
import type { Thread } from '$lib/stores/threads';
import type { ContextAttachTarget } from '$lib/stores/contextAttach';
import type { View } from '$lib/homePaneConfig';
import { workspaceSettingsOpen } from '$lib/stores/workspaceSettings';

export interface CompactArtifactHeader {
	title: string;
	tabCount: number;
	anyChanged: boolean;
	onBack: () => void;
	onTabs: () => void;
}

interface CanvasHeaderInput {
	projectSettingsOpen: boolean;
	threadSettingsOpen: boolean;
	workspaceSettingsOpen: boolean;
	/** Present only when the canvas is too narrow to split and an artifact is active. */
	compactArtifact: CompactArtifactHeader | null;
	contextAttachTarget: ContextAttachTarget | null;
	activeView: View;
	uiScopeName: string | null;
	activeThreadId: string | null;
	conductorThreadId: string | null;
	threads: Thread[];
	closeProjectSettings: () => void;
	closeThreadSettings: () => void;
	closeSettings: () => void;
	closeDocuments: () => void;
}

export function syncCanvasHeader(input: CanvasHeaderInput) {
	if (input.projectSettingsOpen) {
		canvasHeader.set({
			title: 'Project settings',
			backLabel: input.uiScopeName ?? 'Workspace',
			onBack: input.closeProjectSettings,
		});
		return;
	}
	if (input.workspaceSettingsOpen) {
		canvasHeader.set({
			title: 'Workspace settings',
			backLabel: 'Workspace',
			onBack: () => workspaceSettingsOpen.set(false),
		});
		return;
	}
	if (input.threadSettingsOpen) {
		const conductor = input.conductorThreadId != null && input.activeThreadId === input.conductorThreadId;
		const threadTitle = input.activeThreadId
			? input.threads.find((thread) => thread.id === input.activeThreadId)?.title
			: null;
		// The workspace scope's designated thread is the CORSIA agent; a project's is a conductor.
		const designatedName = input.uiScopeName === null ? 'CORSIA' : 'Conductor';
		canvasHeader.set({
			title: conductor ? `${designatedName} settings` : 'Thread settings',
			backLabel: threadTitle ?? (conductor ? designatedName : 'Thread'),
			onBack: input.closeThreadSettings,
		});
		return;
	}
	if (input.activeView === 'activity') {
		canvasHeader.set({ title: input.uiScopeName ?? 'Workspace' });
		return;
	}
	if (input.activeView === 'settings') {
		canvasHeader.set({ title: 'Settings', onBack: input.closeSettings });
		return;
	}
	if (input.activeView === 'documents') {
		const returnTo = input.contextAttachTarget?.returnTo;
		const backLabel =
			returnTo === 'thread-settings' ? 'Thread settings'
			: returnTo === 'project-settings' ? 'Project settings'
			: returnTo === 'workspace-settings' ? 'Workspace settings'
			: undefined;
		canvasHeader.set({ title: 'Documents', backLabel, onBack: input.closeDocuments });
		return;
	}

	// Ranked above the subagent header on purpose: while a document owns the
	// canvas, Back means "back to this thread's chat" (design Back precedence
	// step 2), not "back to the thread that spawned this one". The subagent
	// header returns as soon as chat is the active surface again.
	if (input.activeView === 'chat' && input.compactArtifact) {
		const { title, tabCount, anyChanged, onBack, onTabs } = input.compactArtifact;
		canvasHeader.set({
			title,
			backLabel: 'Chat',
			onBack,
			actions: [{
				id: 'artifact-tabs',
				label: `Tabs (${tabCount})`,
				title: anyChanged ? 'Open artifacts — one has changed' : 'Open artifacts',
				onClick: onTabs,
				active: anyChanged,
				primary: true,
			}],
		});
		return;
	}
	canvasHeader.set(null);
}
