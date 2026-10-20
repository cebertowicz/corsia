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

const loadModels = vi.hoisted(() => vi.fn(async () => {}));
vi.mock('$lib/stores/modelsLoad', () => ({ loadModels }));

const model = (fields: Partial<Model>): Model => ({ id: 'm', name: 'M', size: '', backend: 'network', speed: '', status: 'ready', ...fields } as Model);
const local = model({ id: 'omlx/qwen', name: 'Qwen', litellm_model: 'omlx@qwen', is_local: true });
const cloud = model({ id: 'cc/opus', name: 'Opus', backend: 'claude-code', is_local: false });
const marked = (reason: string) => ({ ...cloud, status: 'unavailable', status_reason: reason }) as Model;

const THREAD_COPY = 'This chat and its agents use local models only.';
const OFF_COPY = 'When on, this chat and its agents use local models only.';
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
		expect(text()).toContain('Local only'); expect(text()).toContain(OFF_COPY);
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
	it('on (own): enabled, switch on, the promise as its second line', () => {
		thread({ local_only: 1, local_only_effective: true, local_only_cause: 'own' }); models.set([local, marked('thread_local_only')]); mountRow();
		expect(toggle().getAttribute('aria-pressed')).toBe('true'); expect(toggle().disabled).toBe(false);
		expect(text()).toContain(THREAD_COPY);
	});
	it('on, inherited: held, "Set by <parent chat title>" links to that chat', () => {
		thread({ local_only: 1, local_only_effective: true, local_only_cause: 'inherited', local_only_inherited_from_thread_id: 'p0', local_only_inherited_from_title: 'Quarterly report' });
		models.set([local, marked('thread_local_only')]);
		const onleave = vi.fn();
		mountRow({ onleave });
		expect(toggle().getAttribute('aria-pressed')).toBe('true'); expect(toggle().disabled).toBe(false);
		expect(toggle().getAttribute('aria-disabled')).toBe('true'); expect(toggle().classList.contains('held')).toBe(true); expect(toggle().classList.contains('locked')).toBe(false);
		expect(text()).toContain('Set by Quarterly report');
		link('Set by Quarterly report')!.click();
		expect(get(openSubagentRequest)).toBe('p0'); expect(onleave).toHaveBeenCalledOnce();
	});
	it('on by its own switch in a cloud-off project stays enabled until it is turned off', () => {
		thread({ local_only: 1, local_only_effective: true, local_only_cause: 'own' });
		models.set([local, marked('thread_local_only')]); mountRow();
		expect(toggle().getAttribute('aria-pressed')).toBe('true');
		expect(toggle().getAttribute('aria-disabled')).toBeNull();
		expect(text()).toContain(THREAD_COPY);
	});
	it('project cause is held and links to project settings', () => {
		thread({ local_only: 0, local_only_effective: true, local_only_cause: 'project' });
		const onleave = vi.fn();
		mountRow({ onleave });
		expect(toggle().getAttribute('aria-disabled')).toBe('true');
		link('Set by this project')!.click();
		expect(get(settingsNavigation)).toMatchObject({ section: 'projects', project: 'p1', anchor: 'project-provider-policy' });
		expect(onleave).toHaveBeenCalledOnce();
	});
});

describe('Local only switch writes', () => {
	it('turns off an own setting in a cloud-off project, then presents the reloaded project cause', async () => {
		thread({ local_only: 1, local_only_effective: true, local_only_cause: 'own' });
		models.set([local, marked('thread_local_only')]);
		const fetchMock = vi.fn(async (input: RequestInfo | URL) => String(input).startsWith('/api/threads?')
			? ({ ok: true, json: async () => ({ threads: [{ id: 't1', user_id: 'u', profile: 'personal', title: 'Chat', model: null, project_id: 'p1', created_at: 'now', updated_at: 'later', local_only: 0, local_only_effective: true, local_only_cause: 'project' }] }) })
			: ({ ok: true }));
		vi.stubGlobal('fetch', fetchMock);
		mountRow();
		toggle().click(); await settle();
		expect(fetchMock).toHaveBeenCalledWith('/api/threads/t1/local-only', expect.objectContaining({ body: JSON.stringify({ local_only: false }) }));
		expect(text()).toContain('Set by this project');
		expect(toggle().getAttribute('aria-disabled')).toBe('true');
		expect(loadModels).toHaveBeenCalledOnce();
	});
	it('on a failed save, reverts the switch and shows the save-failure line', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch'); }));
		mountRow();
		toggle().click(); await settle();
		expect(toggle().getAttribute('aria-pressed')).toBe('false');
		expect(text()).toContain("Couldn’t save this change. Check your connection and try again.");
		expect(host.querySelector('[role="alert"]')).not.toBeNull();
		expect(loadModels).not.toHaveBeenCalled();
	});
});

describe('Local only row focus', () => {
	it('takes focus from its mount prop without a timing window', async () => {
		mountRow({ focusOnMount: true });
		await new Promise((r) => setTimeout(r, 40));
		expect(document.activeElement).toBe(host.querySelector('.local-only-setting'));
	});
});
