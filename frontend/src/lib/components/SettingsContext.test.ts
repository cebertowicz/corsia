import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import SettingsContext from './SettingsContext.svelte';
import { token } from '$lib/stores/auth';
import { coreSettings } from '$lib/stores/settings';
import { projects } from '$lib/stores/projects';
import { threadWorkspaces } from '$lib/stores/artifactWorkspace';
import { activeThreadId } from '$lib/stores/threads';
import { contextAttachTarget } from '$lib/stores/contextAttach';
import { settingsHomeThread } from '$lib/stores/settingsNavigation';
import { showArtifactThreadRequest } from '$lib/openArtifactInThread';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	token.set('test-token');
	activeThreadId.set(null);
	settingsHomeThread.set(null);
	threadWorkspaces.set({});
	projects.set([]);
	coreSettings.set({
		workspace_context_files: [
			{
				source_id: 'workspace-context:AGENTS.md',
				name: 'AGENTS.md',
				label: 'AGENTS.md',
				path: '/Users/alice/Documents/AGENTS.md',
				exists: true,
				editable: false,
				owner_surface: 'document-viewer',
			},
		],
	} as never);
	vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ projects: [] }) })));
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	token.set(null);
	activeThreadId.set(null);
	settingsHomeThread.set(null);
	threadWorkspaces.set({});
	coreSettings.set(null);
	contextAttachTarget.set(null);
	showArtifactThreadRequest.set(null);
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
});

const row = (name: string) => host.querySelector<HTMLElement>(`[data-settings-row="${name}"]`);

describe('SettingsContext', () => {
	it('opens a context file as a tab in the thread Settings was opened over', () => {
		settingsHomeThread.set('home-thread');
		component = mount(SettingsContext, { target: host, props: {} });
		flushSync();

		expect(host.textContent).toContain('All projects');
		row('AGENTS.md')!.querySelector<HTMLButtonElement>('button')!.click();

		// Read-only bootstrap files still need a home thread, so their comments have somewhere to route.
		const tabs = get(threadWorkspaces)['home-thread']?.tabs ?? [];
		expect(tabs).toHaveLength(1);
		expect(tabs[0]).toMatchObject({ path: '/Users/alice/Documents/AGENTS.md', title: 'AGENTS.md' });
		expect(get(showArtifactThreadRequest)).toEqual({ threadId: 'home-thread' });
	});

	it('leaves the row unopenable when no thread can own the tab', () => {
		component = mount(SettingsContext, { target: host, props: {} });
		flushSync();

		expect(row('AGENTS.md')).toBeTruthy();
		expect(row('AGENTS.md')!.querySelector('button')).toBeNull();
		expect(get(threadWorkspaces)).toEqual({});
	});

	it('starts workspace context attach mode and returns to Settings › Context', () => {
		const onBrowseContextFiles = vi.fn();
		component = mount(SettingsContext, { target: host, props: { onBrowseContextFiles } });
		flushSync();

		Array.from(host.querySelectorAll<HTMLButtonElement>('button'))
			.find((button) => button.textContent?.trim() === 'Add existing')
			?.click();

		expect(onBrowseContextFiles).toHaveBeenCalledTimes(1);
		expect(get(contextAttachTarget)).toEqual({
			scope: 'workspace',
			targetLabel: 'Workspace context',
			existingFiles: ['/Users/alice/Documents/AGENTS.md'],
			returnTo: 'settings',
			returnSection: 'context',
		});
	});
});
