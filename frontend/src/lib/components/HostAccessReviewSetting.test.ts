import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import HostAccessReviewSetting from './HostAccessReviewSetting.svelte';
import { token } from '$lib/stores/auth';
import { workspaceModels as models } from '$lib/stores/models';
import { coreSettings } from '$lib/stores/settings';

const FALLBACK_LABEL = "Fall back to the requesting agent's own model when the reviewer can't run";

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
let errors: string[];

const fixed = {
	host_access_reviewer_model: 'review-sonnet',
	host_access_reviewer_model_name: 'Claude Sonnet',
	host_access_reviewer_status: 'ready' as const,
	host_access_reviewer_message: null,
	host_access_reviewer_fallback: false,
	features: {},
};

function render() {
	component = mount(HostAccessReviewSetting, {
		target: host,
		props: { onError: (message: string) => errors.push(message) },
	});
	flushSync();
}

const fallbackToggle = () =>
	host.querySelector<HTMLButtonElement>(`button[aria-label="${FALLBACK_LABEL}"]`);

function patches(fetchMock: ReturnType<typeof vi.fn>): Record<string, unknown>[] {
	return fetchMock.mock.calls
		.filter((call) => (call[1] as RequestInit | undefined)?.method === 'PATCH')
		.map((call) => JSON.parse(String((call[1] as RequestInit).body)));
}

beforeEach(() => {
	errors = [];
	token.set('test-token');
	models.set([
		{ id: 'review-sonnet', name: 'Claude Sonnet', size: 'cloud', backend: 'api', speed: 'fast', status: 'ready' },
	]);
	coreSettings.set(fixed);
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.restoreAllMocks();
	token.set(null);
	models.set([]);
	coreSettings.set(null);
});

describe('HostAccessReviewSetting fallback', () => {
	it('hides the fallback row when no fixed reviewer is chosen', () => {
		coreSettings.set({
			...fixed,
			host_access_reviewer_model: null,
			host_access_reviewer_model_name: null,
			host_access_reviewer_status: 'requester_model',
		});
		render();

		expect(fallbackToggle()).toBeNull();
		expect(host.textContent).not.toContain("Fall back to the agent's own model");
		expect(host.textContent).toContain('Auto-approve leases');
	});

	it('names the chosen reviewer in the fallback row, off by default', () => {
		render();

		expect(fallbackToggle()?.getAttribute('aria-pressed')).toBe('false');
		expect(host.textContent).toContain("Fall back to the agent's own model");
		expect(host.textContent).toContain("If Claude Sonnet can't run");
		expect(host.textContent).toContain('Off: requests fail until Claude Sonnet is back');
	});

	it('saves the switch and disables it while saving', async () => {
		let finish!: (value: Response) => void;
		const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => (
			init?.method === 'PATCH'
				? new Promise<Response>((resolve) => { finish = resolve; })
				: { ok: true, json: async () => ({ ...fixed, host_access_reviewer_fallback: true }) } as Response
		));
		vi.stubGlobal('fetch', fetchMock);
		render();

		fallbackToggle()!.click();
		await tick();

		expect(patches(fetchMock)).toEqual([{ host_access_reviewer_fallback: true }]);
		expect(fallbackToggle()!.disabled).toBe(true);

		finish({ ok: true, json: async () => ({ ok: true }) } as Response);
		await vi.waitFor(() => expect(fallbackToggle()!.getAttribute('aria-pressed')).toBe('true'));
		expect(fallbackToggle()!.disabled).toBe(false);
		expect(errors).toEqual(['']);
	});

	it('puts the switch back and shows the error when the save fails', async () => {
		vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => (
			init?.method === 'PATCH'
				? { ok: false, json: async () => ({ detail: 'This change needs the signed-in person.' }) }
				: { ok: true, json: async () => fixed }
		)));
		render();

		fallbackToggle()!.click();

		await vi.waitFor(() => expect(errors.at(-1)).toBe('This change needs the signed-in person.'));
		expect(fallbackToggle()!.getAttribute('aria-pressed')).toBe('false');
		expect(fallbackToggle()!.disabled).toBe(false);
	});
});

describe('HostAccessReviewSetting status pill', () => {
	const outage = {
		...fixed,
		host_access_reviewer_status: 'unavailable' as const,
		host_access_reviewer_message: "Claude Sonnet can't run right now (usage limit).",
	};

	it('shows Unavailable, in the error color, when the reviewer is out and fallback is off', () => {
		coreSettings.set(outage);
		render();

		expect(host.querySelector('.pill')?.textContent).toBe('Unavailable');
		expect(host.querySelector('.settings-status.error')?.textContent).toContain("can't run right now");
	});

	it('shows Using fallback, not an error, when the reviewer is out and fallback is on', () => {
		coreSettings.set({ ...outage, host_access_reviewer_fallback: true });
		render();

		expect(host.querySelector('.pill')?.textContent).toBe('Using fallback');
		expect(host.querySelector('.settings-status')?.classList.contains('error')).toBe(false);
	});

	it('shows Ready when the reviewer can run, whatever the switch says', () => {
		coreSettings.set({ ...fixed, host_access_reviewer_fallback: true });
		render();

		expect(host.querySelector('.pill')?.textContent).toBe('Ready');
	});
});
