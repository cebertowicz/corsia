import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import SettingsSkills from './SettingsSkills.svelte';
import { token } from '$lib/stores/auth';
import { projects } from '$lib/stores/projects';
import { get } from 'svelte/store';
import { threadWorkspaces } from '$lib/stores/artifactWorkspace';
import { activeThreadId } from '$lib/stores/threads';
import { recordSettingsHomeThread, resetSettingsNavigation } from '$lib/stores/settingsNavigation';
import { showArtifactThreadRequest } from '$lib/openArtifactInThread';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

const skill = (name: string, scope: string) => ({
	name,
	description: `Use when ${name}.`,
	scope,
	scope_id: scope === 'workspace' ? 'workspace' : 'atlas',
	path: `/workspace/${scope}/skills/${name}/SKILL.md`,
	body_path: `/workspace/${scope}/skills/${name}/SKILL.md`,
	metadata: {},
	status: 'included',
	errors: [],
});

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	token.set('test-token');
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
		const path = String(input);
		if (path.includes('/api/skills') && path.includes('project')) {
			return { ok: true, json: async () => ({ skills: [skill('deploy', 'project')], root: '/workspace/atlas', scope_id: 'atlas' }) };
		}
		if (path.includes('/api/skills')) {
			return { ok: true, json: async () => ({ skills: [skill('review', 'workspace')], root: '/workspace', scope_id: 'workspace' }) };
		}
		if (path.startsWith('/api/projects')) {
			return { ok: true, json: async () => ({ projects: [{ id: 'atlas', name: 'Atlas', status: 'active' }] }) };
		}
		return { ok: true, json: async () => ({}) };
	}));
	projects.set([{ id: 'atlas', name: 'Atlas', status: 'active' } as never]);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	token.set(null);
	projects.set([]);
	activeThreadId.set(null);
	resetSettingsNavigation();
	threadWorkspaces.set({});
	showArtifactThreadRequest.set(null);
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
});

describe('SettingsSkills', () => {
	it('shows every level\'s skills on one page, all projects first, and browses for more', async () => {
		const onBrowseSkills = vi.fn();
		component = mount(SettingsSkills, { target: host, props: { onBrowseSkills } });
		flushSync();

		await vi.waitFor(() => {
			expect(host.textContent).toContain('review');
			expect(host.textContent).toContain('deploy');
		});
		const text = host.textContent ?? '';
		expect(text).toContain('Every project and chat gets these. A project or chat skill with the same name takes priority.');
		expect(text).toContain('Chats in this project get these. A chat skill with the same name takes priority.');
		expect(text.indexOf('All projects')).toBeLessThan(text.indexOf('Atlas'));
		expect(host.querySelector('[data-settings-anchor="skills-atlas"]')).toBeTruthy();

		const browse = await vi.waitFor(() => {
			const button = Array.from(host.querySelectorAll<HTMLButtonElement>('button'))
				.find((candidate) => candidate.textContent?.trim() === 'Browse');
			expect(button?.disabled).toBe(false);
			return button!;
		});
		browse.click();
		expect(onBrowseSkills).toHaveBeenCalledTimes(1);
	});

	describe('opening a skill', () => {
		const NOTE = 'Open a chat to view this.';
		const rowButton = (name: string) => host.querySelector<HTMLButtonElement>(`[data-settings-row="${name}"] button`);

		/** The shared stub, plus the two scope-thread lookups answering as given (null: the scope has no thread). */
		function stubScopeThreads(workspaceThread: string | null, projectThread: string | null) {
			const base = (globalThis.fetch as unknown as (input: RequestInfo | URL) => Promise<unknown>);
			const answer = (thread: string | null) => ({ ok: true, json: async () => (thread ? { thread_id: thread } : {}) });
			const stub = vi.fn(async (input: RequestInfo | URL) => {
				const path = String(input);
				if (path.startsWith('/api/corsia-agent')) return answer(workspaceThread);
				if (path.startsWith('/api/conductor')) return answer(projectThread);
				return base(input);
			});
			vi.stubGlobal('fetch', stub);
			return stub;
		}

		const mountLoaded = async () => {
			component = mount(SettingsSkills, { target: host, props: {} });
			flushSync();
			await vi.waitFor(() => expect(host.textContent).toContain('deploy'));
		};

		it('opens beside the thread Settings opened over, without asking for a scope thread', async () => {
			recordSettingsHomeThread('settings', 'home-thread');
			const stub = stubScopeThreads('corsia-thread', 'atlas-conductor');
			await mountLoaded();

			rowButton('review')!.click();
			rowButton('deploy')!.click();

			expect((get(threadWorkspaces)['home-thread']?.tabs ?? []).map((tab) => tab.title)).toEqual(['review', 'deploy']);
			expect(get(showArtifactThreadRequest)).toEqual({ threadId: 'home-thread' });
			expect(stub.mock.calls.some(([url]) => String(url).startsWith('/api/corsia-agent') || String(url).startsWith('/api/conductor'))).toBe(false);
			expect(host.textContent).not.toContain(NOTE);
		});

		it('with no thread on screen, opens beside its scope\'s thread: the CORSIA agent or the project\'s conductor', async () => {
			stubScopeThreads('corsia-thread', 'atlas-conductor');
			await mountLoaded();
			await vi.waitFor(() => expect(rowButton('review')).toBeTruthy());
			await vi.waitFor(() => expect(rowButton('deploy')).toBeTruthy());

			rowButton('review')!.click();
			expect(get(showArtifactThreadRequest)).toEqual({ threadId: 'corsia-thread' });
			rowButton('deploy')!.click();
			expect(get(showArtifactThreadRequest)).toEqual({ threadId: 'atlas-conductor' });

			expect(get(threadWorkspaces)['corsia-thread']?.tabs.map((tab) => tab.title)).toEqual(['review']);
			expect(get(threadWorkspaces)['atlas-conductor']?.tabs.map((tab) => tab.title)).toEqual(['deploy']);
			expect(host.textContent).not.toContain(NOTE);
		});

		it('with no thread on screen and none for a scope, the row is text and says what to do', async () => {
			stubScopeThreads(null, null);
			await mountLoaded();
			await vi.waitFor(() => expect(host.textContent).toContain(NOTE));

			expect(host.querySelector('[data-settings-row="review"]')).toBeTruthy();
			expect(rowButton('review')).toBeNull();
			expect(rowButton('deploy')).toBeNull();
			expect(get(threadWorkspaces)).toEqual({});
		});

		it('is interactive for the scope that has a thread and text for the one that has none', async () => {
			stubScopeThreads('corsia-thread', null);
			await mountLoaded();
			await vi.waitFor(() => expect(rowButton('review')).toBeTruthy());
			await vi.waitFor(() => expect(host.textContent).toContain(NOTE));

			expect(rowButton('deploy')).toBeNull();
			expect(host.textContent?.split(NOTE).length).toBe(2);
		});

		it('a failed lookup says so instead of claiming there is no thread, and Try again asks again', async () => {
			const FAILED = "Couldn't find where to open these.";
			const base = (globalThis.fetch as unknown as (input: RequestInfo | URL) => Promise<unknown>);
			let conductorOk = false;
			vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
				const path = String(input);
				if (path.startsWith('/api/corsia-agent')) return { ok: true, json: async () => ({ thread_id: 'corsia-thread' }) };
				if (path.startsWith('/api/conductor')) {
					return conductorOk ? { ok: true, json: async () => ({ thread_id: 'atlas-conductor' }) } : { ok: false, json: async () => ({}) };
				}
				return base(input);
			}));
			await mountLoaded();
			await vi.waitFor(() => expect(host.textContent).toContain(FAILED));

			expect(rowButton('deploy')).toBeNull();
			expect(host.textContent).not.toContain(NOTE);

			conductorOk = true;
			Array.from(host.querySelectorAll<HTMLButtonElement>('button')).find((button) => button.textContent?.trim() === 'Try again')!.click();
			await vi.waitFor(() => expect(rowButton('deploy')).toBeTruthy());
			expect(host.textContent).not.toContain(FAILED);
		});
	});
});
