import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import WorkspaceSettingsView from './WorkspaceSettingsView.svelte';
import { token } from '$lib/stores/auth';
import { coreSettings } from '$lib/stores/settings';
import { threadWorkspaces } from '$lib/stores/artifactWorkspace';
import { activeThreadId } from '$lib/stores/threads';
import { contextAttachTarget } from '$lib/stores/contextAttach';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

const workspaceSkill = {
	name: 'review',
	description: 'Use when reviewing code.',
	scope: 'workspace',
	scope_id: 'workspace',
	path: '/workspace/skills/review/SKILL.md',
	body_path: '/workspace/skills/review/SKILL.md',
	metadata: {},
	status: 'included',
	errors: [],
};

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	token.set('test-token');
	activeThreadId.set('settings-thread');
	threadWorkspaces.set({});
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
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
		const path = String(input);
		if (path.includes('/api/skills')) {
			return { ok: true, json: async () => ({ skills: [workspaceSkill], root: '/workspace', scope_id: 'workspace' }) };
		}
		if (path.includes('/api/provider-policy')) {
			return { ok: true, json: async () => ({ scope: 'workspace', providers: [] }) };
		}
		return { ok: true, json: async () => ({}) };
	}));
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	token.set(null);
	activeThreadId.set(null);
	threadWorkspaces.set({});
	coreSettings.set(null);
	contextAttachTarget.set(null);
	vi.restoreAllMocks();
});

describe('WorkspaceSettingsView', () => {
	it('manages workspace skills and browses for more', async () => {
		const onBrowseSkills = vi.fn();
		component = mount(WorkspaceSettingsView, { target: host, props: { onBrowseSkills } });
		flushSync();

		await vi.waitFor(() => expect(host.textContent).toContain('review'));
		expect(host.textContent).toContain('Managing skills for Workspace');

		const browse = await vi.waitFor(() => {
			const button = Array.from(host.querySelectorAll<HTMLButtonElement>('button'))
				.find((candidate) => candidate.textContent?.trim() === 'Browse');
			expect(button?.disabled).toBe(false);
			return button!;
		});
		browse.click();
		expect(onBrowseSkills).toHaveBeenCalledTimes(1);
	});

	it('views read-only workspace context source files in the document pane', () => {
		component = mount(WorkspaceSettingsView, { target: host, props: {} });
		flushSync();

		expect(host.textContent).toContain('Workspace context');
		expect(host.textContent).toContain('AGENTS.md');

		const openButton = host.querySelector<HTMLButtonElement>('button[aria-label="View AGENTS.md"]');
		expect(openButton).toBeTruthy();
		openButton?.click();

		// Bootstrap files are read-only, but they still need a home thread so
		// their comments have somewhere to route.
		const tabs = get(threadWorkspaces)['settings-thread']?.tabs ?? [];
		expect(tabs).toHaveLength(1);
		expect(tabs[0]).toMatchObject({ path: '/Users/alice/Documents/AGENTS.md', title: 'AGENTS.md' });
	});

	it('disables View with a reason when no thread can own the artifact tab', () => {
		activeThreadId.set(null);
		component = mount(WorkspaceSettingsView, { target: host, props: {} });
		flushSync();

		const view = host.querySelector<HTMLButtonElement>('button[aria-label="View AGENTS.md"]');
		expect(view?.disabled).toBe(true);
		expect(view?.title).toBe('Open a thread first — documents open beside a conversation');
		view?.click();
		expect(get(threadWorkspaces)).toEqual({});
	});

	it('starts workspace context attach mode in the shared Documents browser', () => {
		const onBrowseContextFiles = vi.fn();
		component = mount(WorkspaceSettingsView, { target: host, props: { onBrowseContextFiles } });
		flushSync();

		Array.from(host.querySelectorAll<HTMLButtonElement>('button'))
			.find((button) => button.textContent?.trim() === 'Add existing')
			?.click();

		expect(onBrowseContextFiles).toHaveBeenCalledTimes(1);
		expect(get(contextAttachTarget)).toEqual({
			scope: 'workspace',
			targetLabel: 'Workspace context',
			existingFiles: ['/Users/alice/Documents/AGENTS.md'],
			returnTo: 'workspace-settings',
		});
	});
});
