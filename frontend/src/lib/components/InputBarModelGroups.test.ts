import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import InputBarModelSheet from './InputBarModelSheet.svelte';
import PickerAccountHeader from './PickerAccountHeader.svelte';
import PickerModelGroupHeader from './PickerModelGroupHeader.svelte';
import { token } from '$lib/stores/auth';
import { activeModelId, models, type Model } from '$lib/stores/models';
import { withAccountTurn, type TurnFields } from '$lib/stores/modelFixtures';
import type { UsageWindow } from '$lib/stores/modelUsage';
import { activeThreadId, threads } from '$lib/stores/threads';
import { vendorAccounts, type VendorAccount } from '$lib/stores/vendorAccounts';

vi.mock('$lib/product_events', () => ({ emitProductEvent: vi.fn() }));

const CAPS = { context_window_max: 200000, supports_vision: true, supports_tools: true, supports_reasoning: true, reasoning_config: { type: 'boolean' as const, default: true } };
const usage = { state: 'allowed' as const, usable: true, windows: [] };
const week = { kind: 'seven_day', state: 'reported' as const, used_percent: 71, resets_at: '2099-01-01T00:00:00Z' };

function row(over: Partial<Model> & TurnFields): Model {
	return withAccountTurn({ id: 'x', name: 'X', size: '', backend: 'cc', provider: 'claude-code', speed: '', status: 'ready', available: true, connection: 'claude-code', capabilities: CAPS, ...over });
}
function account(provider: string, windows: UsageWindow[] = [week]): VendorAccount {
	return { id: 'acct-1', provider, name: 'Work', is_default: false, state: 'signed_in', revision: 1, identity: {}, label: { name: 'Work', email: null, text: 'Work', custom: null }, usage: { state: 'allowed', usable: true, windows, checking: false, check: null } };
}
const CATALOG = [
	row({ id: 'cc/opus', name: 'Claude Code · Opus', display_name: 'Opus', litellm_model: 'claude-code/opus', usage }),
	row({ id: 'cc/sonnet', name: 'Claude Code · Sonnet', display_name: 'Sonnet', litellm_model: 'claude-code/sonnet', usage }),
	row({ id: 'cx/sol', name: 'Codex · Sol', display_name: 'Sol', connection: 'codex', provider: 'codex', backend: 'codex', litellm_model: 'codex/sol', usage }),
	row({ id: 'om/q', name: 'Qwen', display_name: 'Qwen', connection: 'omlx', provider: 'omlx', backend: 'network', server_name: 'This Mac', litellm_model: 'omlx@s/q', is_local: true }),
];
let host: HTMLElement;
let component: Record<string, unknown> | null = null;
const originalFetch = globalThis.fetch;
const open = () => { component = mount(InputBarModelSheet, { target: host }); flushSync(); };
const text = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim();

beforeEach(() => {
	host = document.createElement('div'); document.body.appendChild(host);
	token.set('tok'); activeThreadId.set('thread-1'); threads.set([{ id: 'thread-1', model: 'claude-code/opus', updated_at: '2026-07-17T10:00:00.000000' } as never]); activeModelId.set('claude-code/opus');
	models.set(CATALOG); vendorAccounts.set([account('claude-code'), account('codex')]); globalThis.fetch = vi.fn() as unknown as typeof fetch;
});
afterEach(() => { if (component) unmount(component); component = null; host.remove(); globalThis.fetch = originalFetch; vendorAccounts.set([]); });

describe('model picker groups', () => {
	it('uses one model-first account header per account-backed vendor and preserves local separator copy', () => {
		open();
		expect([...host.querySelectorAll('.picker-account-header')].map((header) => text(header))).toEqual(['Claude Code · Work wk 29% left', 'Codex · Work wk 29% left']);
		expect(text(host.querySelector('[aria-label="This Mac"] .local-status'))).toBe('· Local · ready');
		expect([...host.querySelectorAll('.model-list-name')].map((el) => el.textContent)).toEqual(['Opus', 'Sonnet', 'Sol', 'Qwen']);
	});

	it('keeps the same model capability icons and no provider-specific model chrome', () => {
		open();
		const labels = (row: Element | null) => [...(row?.querySelectorAll('.model-caps [aria-label]') ?? [])].map((icon) => icon.getAttribute('aria-label'));
		expect(labels(host.querySelector('[aria-label="Claude Code"] .model-list-item'))).toEqual(['Reasoning', 'Vision', 'Tools']);
		expect(labels(host.querySelector('[aria-label="This Mac"] .model-list-item'))).toEqual(['Reasoning', 'Vision', 'Tools']);
		expect(host.querySelector('.cc-mark')).toBeNull();
	});

	it('shows a single limited account as back, not first back, and leaves the inventory visible', () => {
		models.set([row({ id: 'cc/opus', display_name: 'Opus', name: 'Claude Code · Opus', status: 'unavailable', status_reason: 'usage_limited', status_resets_at: '2099-01-01T00:00:00Z', usage })]);
		const limited = account('claude-code');
		limited.usage = { state: 'limited', usable: false, resets_at: '2099-01-01T00:00:00Z', windows: [], checking: false, check: null };
		vendorAccounts.set([limited]);
		open();
		const limitedGroup = host.querySelector('[data-state="limited"]')!;
		expect(text(limitedGroup.querySelector('.picker-account-header'))).toMatch(/^Claude Code · Work limited · back /);
		expect(limitedGroup.querySelector('button.picker-account-title')).toBeNull();
		expect(limitedGroup.querySelector('.account-usage-row.marked')).toBeNull();
		expect(limitedGroup.querySelectorAll('.model-list-item')).toHaveLength(1);
		expect(limitedGroup.querySelector('.model-list-item')).toHaveProperty('disabled', true);
	});

	it('shows a signed-out vendor with the existing settings recovery action', () => {
		models.set([row({ status: 'unavailable', status_reason: 'connection_unavailable', usage })]); vendorAccounts.set([]); open();
		const signedOut = host.querySelector('[data-state="signed_out"]')!;
		expect(text(signedOut.querySelector('.picker-account-header'))).toContain('signed out Sign in');
		expect(signedOut.querySelectorAll('.model-list-item')).toHaveLength(1);
	});

	it('opens the usage breakdown with its resets on tap for one-account picker headers', () => {
		// Noon UTC is Jan 15 from UTC-11 to UTC+11, so hosted (UTC) and Mac (Pacific) runners agree.
		const resets_at = '2099-01-15T12:00:00Z';
		models.set([CATALOG[0]]); vendorAccounts.set([account('claude-code', [{ kind: 'five_hour', state: 'reported', used_percent: 42, resets_at }, { ...week, used_percent: 92, resets_at }])]); open();
		const header = host.querySelector('.picker-account-header')!;
		const usage = header.querySelector<HTMLButtonElement>('button.usage')!;
		expect(text(header)).toContain('5h 58% left · wk 8% left');
		expect(header.querySelector('.red')?.textContent).toBe('wk 8% left');
		usage.click(); flushSync();
		expect(text(header.querySelector('.picker-account-usage'))).toContain('resets Jan 15');
	});

	it('gives noninteractive account and group usage one valid accessible name', () => {
		models.set([CATALOG[0]]); vendorAccounts.set([account('claude-code', [{ kind: 'five_hour', state: 'reported', used_percent: 42, resets_at: '2099-01-01T00:00:00Z' }, { ...week, used_percent: 92 }])]); open();
		const accountHost = document.createElement('div'); document.body.appendChild(accountHost);
		const accountHeader = mount(PickerAccountHeader, { target: accountHost, props: { vendor: 'Claude Code', segments: [{ text: '5h 58% left', label: '5-hour: 58% left', level: 'normal' }, { text: 'wk 8% left', label: 'weekly: 8% left', level: 'red' }] } });
		flushSync();
		const accountUsage = accountHost.querySelector('.usage')!;
		expect(accountUsage.getAttribute('role')).toBe('group');
		expect(accountUsage.getAttribute('aria-label')).toBe('5-hour: 58% left, weekly: 8% left');
		expect(accountUsage.querySelector('[aria-hidden="true"]')?.textContent).toContain('5h 58% left');
		const groupHost = document.createElement('div'); document.body.appendChild(groupHost);
		const group = mount(PickerModelGroupHeader, { target: groupHost, props: { name: 'Other', groupKey: 'other', count: 1, status: [{ text: '5h 58% left', label: '5-hour: 58% left', level: 'normal' }, { text: 'wk 8% left', label: 'weekly: 8% left', level: 'red' }] } });
		flushSync();
		const groupUsage = groupHost.querySelector('.group-status')!;
		expect(groupUsage.getAttribute('role')).toBe('group');
		expect(groupUsage.getAttribute('aria-label')).toBe('5-hour: 58% left, weekly: 8% left');
		expect(groupUsage.querySelector('[aria-hidden="true"]')?.textContent).toContain('5h 58% left');
		unmount(accountHeader); accountHost.remove();
		unmount(group); groupHost.remove();
	});

	it('keeps unavailable row notes and the empty state', () => {
		models.set([CATALOG[0], row({ id: 'cc/noctx', display_name: 'NoCtx', name: 'Claude Code · NoCtx', litellm_model: 'claude-code/noctx', status: 'unavailable', status_reason: 'missing_context_window' })]); open();
		expect(text(host.querySelector('.row-note'))).toBe('no context window');
		unmount(component!); component = null; models.set([]); open();
		expect(host.textContent).toContain('No available models');
	});
});
