import { writable } from 'svelte/store';
import type { SettingsSection } from './settingsNavigation';

export type ContextAttachTarget = {
	scope: 'workspace' | 'project' | 'thread';
	projectId?: string | null;
	threadId?: string | null;
	targetLabel?: string | null;
	existingFiles?: string[];
	returnTo?: 'settings' | 'thread-settings' | null;
	/** With returnTo 'settings': the section the pick started from. */
	returnSection?: SettingsSection | null;
};

export const contextAttachTarget = writable<ContextAttachTarget | null>(null);
