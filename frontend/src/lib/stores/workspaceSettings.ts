import { writable } from 'svelte/store';
import { projectSettingsOpen } from './projectSettings';
import { threadSettingsOpen } from './threadSettings';

export const workspaceSettingsOpen = writable(false);

/** Close every scope's settings pane: thread, project and workspace. */
export function closeScopeSettings(): void {
	projectSettingsOpen.set(false);
	threadSettingsOpen.set(false);
	workspaceSettingsOpen.set(false);
}
