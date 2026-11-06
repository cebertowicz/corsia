import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import InputBarModelSheet from './InputBarModelSheet.svelte';
import { token } from '$lib/stores/auth';
import { composerSelectionFor } from '$lib/stores/composerSelection';
import { _resetThreadModelTruthForTests, activeModelId, _resetThreadModelRequestsForTests, models } from '$lib/stores/models';
import { activeThreadId, threads } from '$lib/stores/threads';

vi.mock('$lib/product_events', () => ({ emitProductEvent: vi.fn() }));
vi.mock('$lib/stores/localPickerStatus', async () => {
	const { readable } = await import('svelte/store');
	return { localStatus: readable(null), canAddLocalModel: readable(false), canSetUpLocalModel: readable(false), neverLocalHost: readable(false), retryLocalSetup: vi.fn(), watchLocalStatus: () => () => {} };
});

let host: HTMLElement;
let component: ReturnType<typeof mount> | undefined;
let finishResponse: (response: Response) => void;
const originalFetch = globalThis.fetch;

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	_resetThreadModelTruthForTests();
	_resetThreadModelRequestsForTests();
	token.set('synthetic-test-token');
	activeThreadId.set('click-thread');
	activeModelId.set('previous-model');
	threads.set([{ id: 'click-thread', model: 'previous-model', updated_at: '2026-11-06T19:00:00Z' } as never]);
	models.set([{ id: 'target-model', name: 'Target model', desc: '', size: '', backend: 'network', provider: 'omlx', speed: '', status: 'ready', litellm_model: 'omlx@fixture/target' }]);
	globalThis.fetch = vi.fn(() => new Promise<Response>((resolve) => { finishResponse = resolve; })) as typeof fetch;
});

afterEach(async () => {
	finishResponse?.(new Response(JSON.stringify({ model: 'omlx@fixture/target', model_updated_at: '2026-11-06T19:00:01Z' }), { status: 200 }));
	await Promise.resolve();
	await Promise.resolve();
	if (component) await unmount(component);
	component = undefined;
	host.remove();
	_resetThreadModelRequestsForTests();
	globalThis.fetch = originalFetch;
});

it('paints pending immediately on click while PATCH and server events are unresolved', async () => {
	component = mount(InputBarModelSheet, { target: host, props: { threadId: 'click-thread' } });
	flushSync();
	const button = host.querySelector<HTMLButtonElement>('button.model-list-item')!;
	expect(button).not.toBeNull();
	button.click();
	await Promise.resolve();
	flushSync();
	expect(globalThis.fetch).toHaveBeenCalledWith(expect.stringContaining('/api/threads/click-thread'), expect.objectContaining({ method: 'PATCH' }));
	// A click is observable before either transport can acknowledge it.
	expect(button.querySelector('[aria-label="Switching model"]')).not.toBeNull();
	expect(get(composerSelectionFor)('click-thread').switching?.pending_model).toBe('omlx@fixture/target');
});

it('renders the same pending marker once canonical server state arrives', () => {
	threads.update((rows) => rows.map((row) => ({ ...row, pending_model: 'omlx@fixture/target', switch_status: 'preparing', switch_operation_id: 'fixture-op' })));
	component = mount(InputBarModelSheet, { target: host, props: { threadId: 'click-thread' } });
	flushSync();
	expect(host.querySelector('[aria-label="Switching model"]')).not.toBeNull();
});
