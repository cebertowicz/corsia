import { fromStore } from 'svelte/store';
import { canvasHeader } from '$lib/stores/canvasHeader';
import type { Thread } from '$lib/stores/threads';
import type { ContextAttachTarget } from '$lib/stores/contextAttach';
import type { View } from '$lib/homePaneConfig';
import type { SettingsDrill } from '$lib/stores/settingsNavigation';
import { activityPhoneItem } from '$lib/stores/activityPhoneItem';

/**
 * On a phone, the Activity item open on screen: back to the list, and Next. Read here, not passed in, so the effect
 * that calls syncCanvasHeader re-runs when it changes.
 */
const phoneItem = fromStore(activityPhoneItem);

export interface CompactArtifactHeader {
	title: string;
	tabCount: number;
	anyChanged: boolean;
	onBack: () => void;
	onTabs: () => void;
}

interface CanvasHeaderInput {
	threadSettingsOpen: boolean;
	/** Present only when the canvas is too narrow to split and an artifact is active. */
	compactArtifact: CompactArtifactHeader | null;
	contextAttachTarget: ContextAttachTarget | null;
	activeView: View;
	uiScopeName: string | null;
	activeThreadId: string | null;
	conductorThreadId: string | null;
	threads: Thread[];
	closeThreadSettings: () => void;
	closeSettings: () => void;
	closeDocuments: () => void;
	/** A drilled Settings page names itself and goes up one level. */
	settingsDrill?: SettingsDrill | null;
}

export function syncCanvasHeader(input: CanvasHeaderInput) {
	if (input.threadSettingsOpen) {
		const conductor = input.conductorThreadId != null && input.activeThreadId === input.conductorThreadId;
		const threadTitle = input.activeThreadId
			? input.threads.find((thread) => thread.id === input.activeThreadId)?.title
			: null;
		// The workspace scope's designated thread is the CORSIA agent; a project's is a conductor.
		const designatedName = threadTitle || (input.uiScopeName === null ? 'CORSIA' : 'Conductor');
		canvasHeader.set({
			title: conductor ? `${designatedName} settings` : 'Chat settings',
			backLabel: threadTitle ?? (conductor ? designatedName : 'Thread'),
			onBack: input.closeThreadSettings,
		});
		return;
	}
	if (input.activeView === 'activity') {
		const item = phoneItem.current;
		canvasHeader.set(item
			? {
				title: '',
				backLabel: item.backLabel,
				links: true,
				onBack: item.onBack,
			}
			: { title: 'Activity' });
		return;
	}
	if (input.activeView === 'settings') {
		const drill = input.settingsDrill;
		canvasHeader.set(drill
			? { title: drill.title, backLabel: drill.backLabel, onBack: drill.onBack }
			: { title: 'Settings', onBack: input.closeSettings });
		return;
	}
	if (input.activeView === 'documents') {
		const returnTo = input.contextAttachTarget?.returnTo;
		const backLabel =
			returnTo === 'thread-settings' ? 'Chat settings'
			: returnTo === 'settings' ? 'Settings'
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
		// The name opens the open-files list (a ⌄ after it says so); the document's own controls join the bar from its header.
		canvasHeader.set({
			title,
			backLabel: 'Chat',
			onBack,
			titleAction: {
				label: `Open files, ${tabCount}`,
				title: anyChanged ? 'Show open files — one has changed' : 'Show open files',
				onClick: onTabs,
				changed: anyChanged,
			},
		});
		return;
	}
	canvasHeader.set(null);
}
