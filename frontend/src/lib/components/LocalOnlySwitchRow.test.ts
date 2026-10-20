import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import LocalOnlySwitchRow from './LocalOnlySwitchRow.svelte';
import { token } from '$lib/stores/auth';
import { activeModelId, models, type Model } from '$lib/stores/models';
import { workspaceModels } from '$lib/stores/modelsState';
import { activeThreadId, threads, type Thread } from '$lib/stores/threads';
import { openSubagentRequest } from '$lib/stores/subagents';
import { settingsNavigation } from '$lib/stores/settingsNavigation';
import { requestModelSheet } from '$lib/stores/modelSheetRequest';

const loadModels = vi.hoisted(() => vi.fn(async () => {}));
vi.mock('$lib/stores/modelsLoad', () => ({ loadModels }));

const model = (fields: Partial<Model>): Model => ({ id: 'm', name: 'M', size: '', backend: 'network', speed: '', status: 'ready', ...fields } as Model);
const local = model({ id: 'omlx/qwen', name: 'Qwen', litellm_model: 'omlx@qwen', is_local: true });
const cloud = model({ id: 'cc/opus', name: 'Opus', backend: 'claude-code', is_local: false });
const marked = (reason: string) => ({ ...cloud, status: 'unavailable', status_reason: reason }) as Model;

const THREAD_COPY = 'This chat and its workers use local models only.';
let host: HTMLElement;
let component: Record<string, unknown> | null = null;
const text = () => (host.textContent ?? '').replace(/\s+/g, ' ').trim();
const toggle = () => host.querySelector<HTMLButtonElement>('button[aria-label="Local only"]')!;
const link = (label: string) => Array.from(host.querySelectorAll('button')).find((b) => b.textContent?.trim().startsWith(label));
const mountRow = (props: Record<string, unknown> = {}) => { component = mount(LocalOnlySwitchRow, { target: host, props }); flushSync(); };
const thread = (fields: Partial<Thread> = {}) => threads.set([{
	id: 't1', user_id: 'u', profile: 'personal', title: 'Chat', model: null, project_id: 'p1', created_at: 'now', updated_at: 'now', ...fields,
}]);
const settle = async () => { await Promise.resolve(); await Promise.resolve(); await new Promise((r) => setTimeout(r, 0)); flushSync(); };

beforeEach(() => {
	host = document.createElement('div'); document.body.appendChild(host);
	token.set('tok'); activeThreadId.set('t1'); activeModelId.set('omlx@qwen');
	models.set([local, cloud]); workspaceModels.set([local, cloud]);
	thread();
	loadModels.mockClear();
});
afterEach(() => {
	if (component) unmount(component);
	component = null; host.remove(); token.set(null); activeThreadId.set(null); threads.set([]);
	vi.unstubAllGlobals();
});

describe('Local only switch row states', () => {
	it('off, on a local model: enabled, with the promise as its second line', () => {
		mountRow();
		expect(text()).toContain('Local only'); expect(text()).toContain(THREAD_COPY);
		expect(toggle().getAttribute('aria-pressed')).toBe('false'); expect(toggle().disabled).toBe(false);
	});
	it('off, on a cloud model: disabled, naming the fix', () => {
		activeModelId.set('cc/opus'); mountRow();
		expect(text()).toContain('Switch this chat to a local model first.');
		expect(toggle().disabled).toBe(true);
	});
	it('off, no local model ready: disabled, with the setup link', () => {
		models.set([cloud]); mountRow();
		expect(text()).toContain('No local model is ready.');
		expect(toggle().disabled).toBe(true);
		expect(link('Set up a local model')).toBeTruthy();
	});
	it('the setup link calls the sheet\'s add-local-model entry point', () => {
		models.set([cloud]);
		const onsetuplocal = vi.fn();
		mountRow({ onsetuplocal });
		link('Set up a local model')!.click();
		expect(onsetuplocal).toHaveBeenCalledOnce();
	});
	it('on (own): enabled, switch on, the promise as its second line', () => {
		thread({ local_only: 1 }); models.set([local, marked('thread_local_only')]); mountRow();
		expect(toggle().getAttribute('aria-pressed')).toBe('true'); expect(toggle().disabled).toBe(false);
		expect(text()).toContain(THREAD_COPY);
	});
	it('on, inherited: locked, "Set by <parent chat title>" links to that chat', () => {
		thread({ local_only: 1, local_only_inherited_from_thread_id: 'p0', local_only_inherited_from_title: 'Quarterly report' });
		models.set([local, marked('thread_local_only')]);
		const onleave = vi.fn();
		mountRow({ onleave });
		expect(toggle().getAttribute('aria-pressed')).toBe('true'); expect(toggle().disabled).toBe(false);
		expect(toggle().getAttribute('aria-disabled')).toBe('true'); expect(toggle().getAttribute('aria-describedby')).toBe('local-only-description');
		expect(text()).toContain('Set by Quarterly report');
		link('Set by Quarterly report')!.click();
		expect(get(openSubagentRequest)).toBe('p0'); expect(onleave).toHaveBeenCalledOnce();
	});
	it('on, from the project: locked, "Set by this project" links to project settings', () => {
		models.set([local, marked('project_cloud_off')]);
		const onleave = vi.fn();
		mountRow({ onleave });
		expect(toggle().getAttribute('aria-pressed')).toBe('true'); expect(toggle().disabled).toBe(false);
		expect(toggle().getAttribute('aria-disabled')).toBe('true');
		link('Set by this project')!.click();
		expect(get(settingsNavigation)).toMatchObject({ section: 'projects', project: 'p1', anchor: 'project-provider-policy' });
		expect(onleave).toHaveBeenCalledOnce();
	});
	it('inherited, with the parent since turned off, stays on and locked', () => {
		thread({ local_only: 1, local_only_inherited_from_thread_id: 'p0', local_only_inherited_from_title: 'Quarterly report', local_only_effective: true });
		mountRow();
		expect(toggle().getAttribute('aria-pressed')).toBe('true'); expect(toggle().disabled).toBe(false);
		expect(toggle().getAttribute('aria-disabled')).toBe('true');
	});
	it('renders no row without a thread', () => {
		activeThreadId.set(null); mountRow();
		expect(text()).toBe('');
	});
});

describe('Local only switch writes', () => {
	const respond = (ok: boolean, body: unknown = {}) => vi.fn(() => new Promise<Response>((resolve) => {
		resolver = () => resolve({ ok, status: ok ? 200 : 500, json: async () => body } as Response);
	}));
	let resolver = () => {};

	it('moves at once, shows Saving…, filters the list to local rows, and waits for the save before the badge state', async () => {
		const fetchMock = respond(true, { ok: true, local_only: true });
		vi.stubGlobal('fetch', fetchMock);
		mountRow();
		toggle().click(); flushSync();
		expect(toggle().getAttribute('aria-pressed')).toBe('true');
		expect(text()).toContain('Saving…');
		expect(toggle().getAttribute('aria-disabled')).toBe('true');
		expect(get(models).find((m) => m.id === 'cc/opus')?.status_reason).toBe('thread_local_only');
		expect(get(threads)[0].local_only).toBeUndefined();
		toggle().click(); flushSync();
		expect(fetchMock).toHaveBeenCalledTimes(1);
		resolver(); await settle();
		expect(text()).not.toContain('Saving…');
		expect(get(threads)[0]).toMatchObject({ local_only: true });
		expect(loadModels).toHaveBeenCalledOnce();
	});
	it('on a failed save, reverts the switch and the list and shows the save-failure line', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch'); }));
		mountRow();
		toggle().click(); await settle();
		expect(toggle().getAttribute('aria-pressed')).toBe('false');
		expect(text()).toContain("Couldn't save this change. Check your connection and try again.");
		expect(host.querySelector('[role="alert"]')).not.toBeNull();
		expect(get(models).find((m) => m.id === 'cc/opus')?.status_reason).toBeUndefined();
		expect(loadModels).not.toHaveBeenCalled();
	});
	it('turning it off restores the cloud rows at once and reads the catalog once saved', async () => {
		thread({ local_only: 1 }); models.set([local, marked('thread_local_only')]);
		const fetchMock = respond(true, { ok: true, local_only: false });
		vi.stubGlobal('fetch', fetchMock);
		mountRow();
		toggle().click(); flushSync();
		expect(toggle().getAttribute('aria-pressed')).toBe('false');
		expect(get(models).find((m) => m.id === 'cc/opus')?.status).toBe('ready');
		resolver(); await settle();
		expect(get(threads)[0]).toMatchObject({ local_only: false });
		expect(loadModels).toHaveBeenCalledOnce();
	});
});

describe('Local only row focus', () => {
	it('takes focus when the badge asked for the sheet just before it mounted', async () => {
		requestModelSheet({ focusLocalOnly: true });
		mountRow();
		await new Promise((r) => setTimeout(r, 40));
		expect(document.activeElement).toBe(host.querySelector('.local-only-setting'));
	});
});
