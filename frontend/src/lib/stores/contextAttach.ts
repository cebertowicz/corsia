import { writable } from 'svelte/store';

export type ContextAttachTarget = {
	scope: 'workspace' | 'project' | 'thread';
	projectId?: string | null;
	threadId?: string | null;
	targetLabel?: string | null;
	existingFiles?: string[];
	returnTo?: 'workspace-settings' | 'project-settings' | 'thread-settings' | null;
};

export const contextAttachTarget = writable<ContextAttachTarget | null>(null);
