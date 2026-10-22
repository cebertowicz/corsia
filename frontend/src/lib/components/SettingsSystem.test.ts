import { flushSync, mount, tick, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/version', async () => {
	const actual = await vi.importActual<typeof import('$lib/version')>('$lib/version');
	return { ...actual, reloadWithCacheBust: vi.fn() };
});

import SettingsSystem from './SettingsSystem.svelte';
import { token } from '$lib/stores/auth';
import { workspaceModels as models } from '$lib/stores/models';
import { coreSettings, loadSettings, settingsLoading, updateSettings, updateStatus } from '$lib/stores/settings';
import { threadWorkspaces } from '$lib/stores/artifactWorkspace';
import { activeThreadId } from '$lib/stores/threads';
import { contextAttachTarget } from '$lib/stores/contextAttach';
import { reloadWithCacheBust } from '$lib/version';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

const settingsPayload = {
	summary_default_model: 'omlx@local/qwen',
	summary_default_model_name: 'Qwen Local',
	summary_generation_status: 'active' as const,
	summary_generation_message: null,
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
	features: {},
};

beforeEach(() => {
	vi.mocked(reloadWithCacheBust).mockClear();
	token.set('test-token');
	models.set([
		{
			id: 'summary-test-qwen',
			name: 'Qwen Local',
			size: '35B',
			backend: 'network',
			speed: 'medium',
			status: 'ready',
			litellm_model: 'omlx@local/qwen',
		},
		{
			id: 'summary-test-sonnet',
			name: 'Claude Sonnet',
			size: 'cloud',
			backend: 'api',
			speed: 'fast',
			status: 'ready',
			litellm_model: 'anthropic/claude-3-5-sonnet',
		},
	]);
	coreSettings.set(settingsPayload);
	threadWorkspaces.set({});
	activeThreadId.set('settings-thread');
	contextAttachTarget.set(null);
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
	updateStatus.set(null);
	threadWorkspaces.set({});
	activeThreadId.set(null);
	contextAttachTarget.set(null);
});

describe('SettingsSystem', () => {
	it('does not expose the removed desktop server reset', () => {
		component = mount(SettingsSystem, { target: host, props: { systemStatus: {} } });
		flushSync();
		expect(host.textContent).not.toContain('Reset URL');
		expect(host.textContent).not.toContain('server picker');
	});

	it("labels the supported requester-model reviewer default without a warning", () => {
		coreSettings.set({
			...settingsPayload,
			host_access_reviewer_model: null,
			host_access_reviewer_model_name: null,
			host_access_reviewer_status: 'requester_model' as const,
			host_access_reviewer_message: "Using the requesting agent's model.",
		});
		component = mount(SettingsSystem, { target: host });
		expect(host.querySelector('[data-settings-row="Reviewer model"] .sub')?.textContent).toBe("Using the requesting agent's model.");
		expect(host.querySelector('[data-settings-row="Reviewer model"] .sub.failed')).toBeNull();
		expect(host.textContent).not.toContain('Not configured');
	});

	it('applies Focus mode from a successful PATCH when the readback fails', async () => {
		coreSettings.set({ ...settingsPayload, features: { focus_mode: false } });
		vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
			if (init?.method === 'PATCH') {
				return { ok: true, json: async () => ({ ok: true, features: { focus_mode: true } }) };
			}
			throw new Error('readback unavailable');
		}));

		await expect(updateSettings({ features: { focus_mode: true } })).resolves.toEqual({ ok: true });
		await tick();
		expect(get(coreSettings)?.features?.focus_mode).toBe(true);
	});

	it('does not let a stale GET restore Focus after an Off PATCH', async () => {
		coreSettings.set({ ...settingsPayload, features: { focus_mode: true } });
		let resolveGet: ((response: { ok: boolean; json: () => Promise<unknown> }) => void) | undefined;
		vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
			if (init?.method === 'PATCH') {
				return { ok: true, json: async () => ({ ok: true, features: { focus_mode: false } }) };
			}
			return new Promise((resolve) => { resolveGet = resolve; });
		}));

		const staleRead = loadSettings();
		await expect(updateSettings({ features: { focus_mode: false } })).resolves.toEqual({ ok: true });
		resolveGet?.({ ok: true, json: async () => ({ ...settingsPayload, features: { focus_mode: true } }) });
		await staleRead;

		expect(get(coreSettings)?.features?.focus_mode).toBe(false);
		expect(get(settingsLoading)).toBe(false);
	});

	it('serializes Focus and Debug feature PATCHes in invocation order', async () => {
		coreSettings.set({ ...settingsPayload, features: { focus_mode: false, debug_version_overlay: false } });
		let resolveFocus: ((response: { ok: boolean; json: () => Promise<unknown> }) => void) | undefined;
		let calls = 0;
		const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
			if (init?.method !== 'PATCH') throw new Error('unexpected readback');
			calls += 1;
			if (calls === 1) return new Promise((resolve) => { resolveFocus = resolve; });
			return { ok: true, json: async () => ({ ok: true, features: { focus_mode: true, debug_version_overlay: true } }) };
		});
		vi.stubGlobal('fetch', fetchMock);

		const focus = updateSettings({ features: { focus_mode: true } });
		const debug = updateSettings({ features: { debug_version_overlay: true } });
		await Promise.resolve();
		expect(fetchMock).toHaveBeenCalledTimes(1);
		resolveFocus?.({ ok: true, json: async () => ({ ok: true, features: { focus_mode: true, debug_version_overlay: false } }) });
		await focus;
		await debug;
		expect(get(coreSettings)?.features).toMatchObject({ focus_mode: true, debug_version_overlay: true });
	});

	it('runs a later feature PATCH after an earlier one fails', async () => {
		coreSettings.set({ ...settingsPayload, features: { focus_mode: false, debug_version_overlay: false } });
		let calls = 0;
		vi.stubGlobal('fetch', vi.fn(async () => {
			calls += 1;
			return calls === 1
				? { ok: false, json: async () => ({ detail: 'Focus rejected' }) }
				: { ok: true, json: async () => ({ ok: true, features: { focus_mode: false, debug_version_overlay: true } }) };
		}));

		const focus = updateSettings({ features: { focus_mode: true } });
		const debug = updateSettings({ features: { debug_version_overlay: true } });
		await expect(focus).resolves.toMatchObject({ ok: false });
		await expect(debug).resolves.toEqual({ ok: true });
		expect(get(coreSettings)?.features).toMatchObject({ focus_mode: false, debug_version_overlay: true });
	});

	it('keeps the prior Focus mode setting after a failed PATCH', async () => {
		coreSettings.set({ ...settingsPayload, features: { focus_mode: true } });
		vi.stubGlobal('fetch', vi.fn(async () => ({
			ok: false,
			json: async () => ({ detail: 'Settings rejected' }),
		})));

		await expect(updateSettings({ features: { focus_mode: false } })).resolves.toMatchObject({ ok: false });
		expect(get(coreSettings)?.features?.focus_mode).toBe(true);
	});

	it('keeps core settings usable without legacy summary controls or requests', async () => {
		const requests: string[] = [];
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input);
			requests.push(url);
			return { ok: true, json: async () => url.includes('/api/skills?')
				? { skills: [], root: '/workspace', scope_id: 'workspace' }
				: settingsPayload };
		}));
		component = mount(SettingsSystem, { target: host, props: { systemStatus: {} } });
		flushSync();
		await tick();
		expect(host.querySelector('#host-access-reviewer-model')).not.toBeNull();
		expect(host.querySelector('#summary-default-model')).toBeNull();
		expect(host.textContent).not.toContain('Recent summaries');
		expect(host.textContent).not.toContain('Summary generation blocked');
		expect(host.textContent).not.toContain('Workspace context');
		expect(host.textContent).not.toContain('Workspace skills');
		expect(host.textContent).not.toContain('Managing skills for Workspace');
		expect(requests.filter((url) => url.includes('/api/summaries'))).toEqual([]);
	});

	it('shows and persists the exact host-access reviewer', async () => {
		const configured = {
			...settingsPayload,
			host_access_reviewer_model: 'summary-test-sonnet',
			host_access_reviewer_model_name: 'Claude Sonnet',
			host_access_reviewer_status: 'ready' as const,
			host_access_reviewer_message: null,
		};
		coreSettings.set(configured);
		const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
			if (init?.method === 'PATCH') {
				return { ok: true, json: async () => ({ ok: true }) };
			}
			return { ok: true, json: async () => configured };
		});
		vi.stubGlobal('fetch', fetchMock);

		component = mount(SettingsSystem, { target: host, props: { systemStatus: {} } });
		flushSync();

		const select = host.querySelector<HTMLSelectElement>('#host-access-reviewer-model');
		expect(select?.value).toBe('summary-test-sonnet');
		expect(host.textContent).toContain('Host access review');
		expect(host.textContent).toContain('Ready');
		expect(host.textContent).not.toContain('Core Agent');

		select!.value = 'summary-test-qwen';
		select!.dispatchEvent(new Event('change', { bubbles: true }));
		await tick();

		await vi.waitFor(() => {
			const patch = fetchMock.mock.calls.find(
				(call) => (call[1] as RequestInit | undefined)?.method === 'PATCH',
			);
			expect(JSON.parse(String((patch?.[1] as RequestInit).body))).toEqual({
				host_access_reviewer_model: 'summary-test-qwen',
			});
		});
	});

	it('uses the canonical toggle to persist host-access auto approval', async () => {
		const configured = {
			...settingsPayload,
			host_access_reviewer_model: 'summary-test-sonnet',
			host_access_reviewer_model_name: 'Claude Sonnet',
			host_access_reviewer_status: 'ready' as const,
			host_access_reviewer_message: null,
			host_access_auto_approve: false,
		};
		coreSettings.set(configured);
		const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
			if (init?.method === 'PATCH') {
				return { ok: true, json: async () => ({ ok: true }) };
			}
			return { ok: true, json: async () => configured };
		});
		vi.stubGlobal('fetch', fetchMock);

		component = mount(SettingsSystem, { target: host, props: { systemStatus: {} } });
		flushSync();

		const toggle = host.querySelector<HTMLButtonElement>(
			'button[aria-label="Automatically approve reviewed host-access leases"]',
		);
		expect(toggle).toBeTruthy();
		expect(toggle?.getAttribute('aria-pressed')).toBe('false');
		expect(host.textContent).toMatch(/Auto-approve(?! leases)/);
		expect(host.textContent).toContain('Per-action review and hard safety limits still apply.');
		expect(host.textContent).toContain('Repository-policy exceptions still require your approval.');

		toggle!.click();
		await tick();

		await vi.waitFor(() => {
			const patch = fetchMock.mock.calls.find(
				(call) => (call[1] as RequestInit | undefined)?.method === 'PATCH',
			);
			expect(JSON.parse(String((patch?.[1] as RequestInit).body))).toEqual({
				host_access_auto_approve: true,
			});
		});
	});

	it('keeps an unavailable configured host-access reviewer visible', async () => {
		const unavailable = {
			...settingsPayload,
			host_access_reviewer_model: 'provider/model-no-longer-ready',
			host_access_reviewer_model_name: 'Prior Reviewer',
			host_access_reviewer_status: 'unavailable' as const,
			host_access_reviewer_message: 'The configured reviewer is unavailable.',
		};
		coreSettings.set(unavailable);
		vi.stubGlobal('fetch', vi.fn(async () => ({
			ok: true,
			json: async () => unavailable,
		})));

		component = mount(SettingsSystem, { target: host, props: { systemStatus: {} } });
		flushSync();

		const select = host.querySelector<HTMLSelectElement>('#host-access-reviewer-model');
		expect(select?.value).toBe('provider/model-no-longer-ready');
		expect(select?.selectedOptions[0]?.textContent).toContain('Prior Reviewer');
		expect(select?.selectedOptions[0]?.textContent).toContain('unavailable');
		expect(host.textContent).toContain('The configured reviewer is unavailable.');
	});

	it('keeps a failed host-access reviewer update visible', async () => {
		const configured = {
			...settingsPayload,
			host_access_reviewer_model: 'summary-test-sonnet',
			host_access_reviewer_model_name: 'Claude Sonnet',
			host_access_reviewer_status: 'ready' as const,
			host_access_reviewer_message: null,
		};
		coreSettings.set(configured);
		vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => (
			init?.method === 'PATCH'
				? {
					ok: false,
					json: async () => ({ detail: 'Reviewer selection is no longer ready.' }),
				}
				: { ok: true, json: async () => configured }
		)));

		component = mount(SettingsSystem, { target: host, props: { systemStatus: {} } });
		flushSync();

		const select = host.querySelector<HTMLSelectElement>('#host-access-reviewer-model')!;
		select.value = 'summary-test-qwen';
		select.dispatchEvent(new Event('change', { bubbles: true }));

		await vi.waitFor(() => {
			expect(host.textContent).toContain('Reviewer selection is no longer ready.');
		});
		expect(select.value).toBe('summary-test-sonnet');
	});

	it('does not let an older failed reviewer update replace newer canonical truth', async () => {
		const configured = {
			...settingsPayload,
			host_access_reviewer_model: 'summary-test-sonnet',
			host_access_reviewer_model_name: 'Claude Sonnet',
			host_access_reviewer_status: 'ready' as const,
			host_access_reviewer_message: null,
		};
		const updated = {
			...configured,
			host_access_reviewer_model: 'summary-test-opus',
			host_access_reviewer_model_name: 'Claude Opus',
		};
		models.update((rows) => [
			...rows,
			{
				id: 'summary-test-opus',
				name: 'Claude Opus',
				size: 'cloud',
				backend: 'api',
				speed: 'fast',
				status: 'ready',
			},
		]);
		coreSettings.set(configured);
		let resolveOlderFailure!: (value: Response) => void;
		const olderFailure = new Promise<Response>((resolve) => {
			resolveOlderFailure = resolve;
		});
		let canonical = configured;
		vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
			if (init?.method === 'PATCH') {
				const body = JSON.parse(String(init.body));
				if (body.host_access_reviewer_model === 'summary-test-qwen') {
					return olderFailure;
				}
				canonical = updated;
				return { ok: true, json: async () => updated } as Response;
			}
			return { ok: true, json: async () => canonical } as Response;
		}));

		component = mount(SettingsSystem, { target: host, props: { systemStatus: {} } });
		flushSync();

		const select = host.querySelector<HTMLSelectElement>('#host-access-reviewer-model')!;
		select.value = 'summary-test-qwen';
		select.dispatchEvent(new Event('change', { bubbles: true }));
		select.value = 'summary-test-opus';
		select.dispatchEvent(new Event('change', { bubbles: true }));
		await vi.waitFor(() => expect(select.value).toBe('summary-test-opus'));

		resolveOlderFailure({
			ok: false,
			json: async () => ({ detail: 'Older reviewer update failed.' }),
		} as Response);

		await vi.waitFor(() => expect(host.textContent).toContain('Older reviewer update failed.'));
		expect(select.value).toBe('summary-test-opus');
	});

	it('offers Update only when an update is available', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => settingsPayload })));

		updateStatus.set(null);
		component = mount(SettingsSystem, { target: host, props: { systemStatus: {} } });
		flushSync();

		const names = () => Array.from(host.querySelectorAll('button')).map((b) => b.textContent?.trim());
		expect(names()).toContain('Restart');
		expect(names()).not.toContain('Update');
	});

	it('surfaces the exact restart operation failure without waiting for a global drain', async () => {
		const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input);
			if (url.includes('/api/system/restart')) {
				return { ok: true, json: async () => ({ ok: true, operation_id: 'restart/op' }) };
			}
			if (url.includes('/api/system/drain?')) {
				if (url.includes('operation_id=restart%2Fop')) {
					return {
						ok: true,
						json: async () => ({
							operation_id: 'restart/op',
							status: 'failed',
							phase: null,
							error: 'restart helper exited with status 1',
						}),
					};
				}
				return { ok: true, json: async () => ({ active: false }) };
			}
			if (url.includes('/api/version')) {
				return {
					ok: true,
					json: async () => ({
						frontend: { full_hash: 'aaa' },
						backend: {
							full_hash: 'aaa',
							start_iso: 'old-start',
							current_head: { full_hash: 'aaa' },
						},
					}),
				};
			}
			if (url.includes('/api/summaries?')) {
				return { ok: true, json: async () => ({ summaries: [] }) };
			}
			return { ok: true, json: async () => settingsPayload };
		});
		vi.stubGlobal('fetch', fetchMock);

		component = mount(SettingsSystem, { target: host, props: { systemStatus: {} } });
		flushSync();

		Array.from(host.querySelectorAll<HTMLButtonElement>('button'))
			.find((b) => b.textContent?.trim() === 'Restart')?.click();
		flushSync();
		Array.from(host.querySelectorAll<HTMLButtonElement>('button'))
			.find((b) => b.textContent?.trim() === 'Confirm')?.click();

		await vi.waitFor(() => {
			expect(fetchMock.mock.calls.some(([input]) =>
				String(input).includes('operation_id=restart%2Fop'))).toBe(true);
			expect(host.textContent).toContain('restart helper exited with status 1');
		});
		expect(reloadWithCacheBust).not.toHaveBeenCalled();
	});

	it('surfaces a missing exact restart operation without retrying the global drain', async () => {
		const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input);
			if (url.includes('/api/system/restart')) {
				return { ok: true, json: async () => ({ ok: true, operation_id: 'missing-op' }) };
			}
			if (url.includes('/api/system/drain?')) {
				return {
					ok: false,
					status: 404,
					json: async () => ({ detail: 'restart operation not found' }),
				};
			}
			if (url.includes('/api/version')) {
				return {
					ok: true,
					json: async () => ({
						frontend: { full_hash: 'aaa' },
						backend: {
							full_hash: 'aaa',
							start_iso: 'old-start',
							current_head: { full_hash: 'aaa' },
						},
					}),
				};
			}
			if (url.includes('/api/summaries?')) return { ok: true, json: async () => ({ summaries: [] }) };
			return { ok: true, json: async () => settingsPayload };
		});
		vi.stubGlobal('fetch', fetchMock);

		component = mount(SettingsSystem, { target: host, props: { systemStatus: {} } });
		flushSync();
		Array.from(host.querySelectorAll<HTMLButtonElement>('button'))
			.find((b) => b.textContent?.trim() === 'Restart')?.click();
		flushSync();
		Array.from(host.querySelectorAll<HTMLButtonElement>('button'))
			.find((b) => b.textContent?.trim() === 'Confirm')?.click();

		await vi.waitFor(() => expect(host.textContent).toContain('Restart operation not found.'));
		expect(fetchMock.mock.calls.some(([input]) =>
			String(input).includes('operation_id=missing-op'))).toBe(true);
		expect(reloadWithCacheBust).not.toHaveBeenCalled();
	});

	it('shows progress from the exact in-flight restart phase', async () => {
		let drainReads = 0;
		let releaseTerminal!: (value: object) => void;
		const terminalStatus = new Promise<object>((resolve) => {
			releaseTerminal = resolve;
		});
		const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input);
			if (url.includes('/api/system/restart')) {
				return { ok: true, json: async () => ({ ok: true, operation_id: 'phase-op' }) };
			}
			if (url.includes('/api/system/drain?')) {
				drainReads += 1;
				return {
					ok: true,
					json: async () => drainReads === 1
						? { operation_id: 'phase-op', status: 'in_flight', phase: 'ready', error: null }
						: terminalStatus,
				};
			}
			if (url.includes('/api/version')) {
				return {
					ok: true,
					json: async () => ({
						frontend: { full_hash: 'aaa' },
						backend: {
							full_hash: 'aaa',
							start_iso: 'old-start',
							current_head: { full_hash: 'aaa' },
						},
					}),
				};
			}
			if (url.includes('/api/summaries?')) return { ok: true, json: async () => ({ summaries: [] }) };
			return { ok: true, json: async () => settingsPayload };
		});
		vi.stubGlobal('fetch', fetchMock);

		component = mount(SettingsSystem, { target: host, props: { systemStatus: {} } });
		flushSync();
		Array.from(host.querySelectorAll<HTMLButtonElement>('button'))
			.find((b) => b.textContent?.trim() === 'Restart')?.click();
		flushSync();
		Array.from(host.querySelectorAll<HTMLButtonElement>('button'))
			.find((b) => b.textContent?.trim() === 'Confirm')?.click();

		await vi.waitFor(() => expect(host.textContent).toContain('Drain complete. Starting replacement…'));
		expect(fetchMock.mock.calls.some(([input]) =>
			String(input).includes('operation_id=phase-op'))).toBe(true);
		releaseTerminal({ operation_id: 'phase-op', status: 'failed', phase: null, error: 'restart stopped' });
		await vi.waitFor(() => expect(host.textContent).toContain('restart stopped'));
		expect(reloadWithCacheBust).not.toHaveBeenCalled();
	});

	it.each([
		['Restart', 'Drain + restart?', 'restart'],
	])('runs %s without pulling or building', async (buttonName, confirmation, mode) => {
		let versionReads = 0;
		const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
			const url = String(input);
			if (url.includes('/api/system/restart')) {
				return { ok: true, json: async () => ({ ok: true, message: 'Restarting.', operation_id: 'restart-op' }) };
			}
			if (url.includes('/api/system/drain?')) {
				return { ok: true, json: async () => ({ status: 'complete', phase: null, error: null }) };
			}
			if (url.includes('/api/version')) {
				versionReads += 1;
				return {
					ok: true,
					json: async () => ({
						frontend: { full_hash: 'aaa' },
						backend: {
							full_hash: 'aaa',
							start_iso: versionReads > 1 ? 'new-start' : 'old-start',
							current_head: { full_hash: 'aaa' },
						},
					}),
				};
			}
			if (url.includes('/api/summaries?')) {
				return { ok: true, json: async () => ({ summaries: [] }) };
			}
			return { ok: true, json: async () => settingsPayload };
		});
		vi.stubGlobal('fetch', fetchMock);

		component = mount(SettingsSystem, { target: host, props: { systemStatus: {} } });
		flushSync();

		Array.from(host.querySelectorAll<HTMLButtonElement>('button'))
			.find((b) => b.textContent?.trim() === buttonName)?.click();
		flushSync();
		expect(host.textContent).toContain(confirmation);

		Array.from(host.querySelectorAll<HTMLButtonElement>('button'))
			.find((b) => b.textContent?.trim() === 'Confirm')?.click();

		await vi.waitFor(() => {
			const call = fetchMock.mock.calls.find(([input]) =>
				String(input).includes('/api/system/restart'));
			expect(call).toBeTruthy();
			expect(String(call![0])).toContain(`mode=${mode}`);
			expect(String(call![0])).not.toContain('mode=update');
			expect(versionReads).toBeGreaterThan(1);
			expect(host.textContent).not.toContain(confirmation);
		});
	});
});
