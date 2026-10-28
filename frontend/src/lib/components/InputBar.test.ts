import { get } from 'svelte/store';
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const wsMocks = vi.hoisted(() => {
	const listeners = new Map<string, Set<(data: any) => void>>();
	return {
		sendMessage: vi.fn(),
		stopTurn: vi.fn(),
		on: vi.fn((event: string, handler: (data: any) => void) => {
			if (!listeners.has(event)) listeners.set(event, new Set());
			listeners.get(event)!.add(handler);
			return () => listeners.get(event)?.delete(handler);
		}),
		emitForTest(event: string, data: any) {
			listeners.get(event)?.forEach(handler => handler(data));
		},
		resetListeners() {
			listeners.clear();
		},
	};
});
const productEventMock = vi.hoisted(() => vi.fn());
const warmToastMock = vi.hoisted(() => vi.fn());

vi.mock('$lib/ws', () => wsMocks);
vi.mock('$lib/product_events', () => ({ emitProductEvent: productEventMock }));
vi.mock('$lib/stores/warmToast', () => ({
	showWarmToast: warmToastMock,
	REBUILD_TOAST_CEILING_MS: 60_000,
}));

import InputBar from './InputBar.svelte';
import { QUEUE_ARM_MS as ARM_MS } from './InputBarActionRow.svelte';
import { token } from '$lib/stores/auth';
import { connectionState } from '$lib/stores/connection';
import { _resetThreadModelTruthForTests, acceptThreadModelTruth, activeModelId, models } from '$lib/stores/models';
import { activeThreadId, messages, threads } from '$lib/stores/threads';
import { subagentMeta } from '$lib/stores/subagents';
import { toasts } from '$lib/stores/toast';
import { applyLifecycle, applyTerminal, _resetTurnStateForTests } from '$lib/ws/turnState';
import { stopRequestedTurns } from '$lib/ws/send';
import { clearAllContextUsage, setContextUsage } from '$lib/stores/contextUsageByThread';
import { resetUpdateStoreForTests, updateStatus } from '$lib/update/store';

const attachment = {
	file_id: 'file-1',
	filename: 'notes.txt',
	mime_type: 'text/plain',
	url: '/api/files/file-1',
	is_image: false,
};

let host: HTMLElement;
let component: Record<string, any> | null = null;
const vibrate = vi.fn(() => true);

beforeEach(() => {
	Object.defineProperty(navigator, 'vibrate', {
		value: vibrate, configurable: true, writable: true,
	});
	vibrate.mockClear();
	_resetTurnStateForTests();
	resetUpdateStoreForTests();
	token.set('token');
	connectionState.set('connected');
	activeThreadId.set(null);
	clearAllContextUsage();
	activeModelId.set('network/model');
	models.set([{
		id: 'network/model',
		name: 'Network Model',
		size: '',
		backend: 'network',
		speed: '',
		status: 'ready',
	}]);
	threads.set([]);
	vi.clearAllMocks();
	wsMocks.resetListeners();
	wsMocks.sendMessage.mockReturnValue('input-1');
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
		return {
			ok: true,
			json: async () => String(input).startsWith('/api/upload')
				? attachment
				: { preview: null },
		};
	}));
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	_resetTurnStateForTests();
	resetUpdateStoreForTests();
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.unstubAllGlobals();
	token.set(null);
	connectionState.set('disconnected');
	activeThreadId.set(null);
	clearAllContextUsage();
	activeModelId.set(null);
	models.set([]);
	threads.set([]);
	subagentMeta.set(new Map());
});

async function mountPreparedComposer({ switchThreadOnSend = true } = {}) {
	activeThreadId.set('thread-1');
	const onSend = vi.fn();
	if (switchThreadOnSend) {
		onSend.mockImplementation(() => activeThreadId.set('thread-after-send'));
	}
	const onAutoCreateThread = vi.fn(async () => {
		activeThreadId.set('thread-1');
	});
	component = mount(InputBar, {
		target: host,
		props: {
			onSend,
			onAutoCreateThread,
		},
	});
	flushSync();

	await component.setText('  revised message  ');
	const fileInput = host.querySelector<HTMLInputElement>('input[type="file"]')!;
	Object.defineProperty(fileInput, 'files', {
		configurable: true,
		value: [new File(['notes'], attachment.filename, { type: attachment.mime_type })],
	});
	fileInput.dispatchEvent(new Event('change', { bubbles: true }));
	await vi.waitFor(() => expect(host.querySelector('.attachment-chip')).toBeTruthy());

	const composer = host.querySelector<HTMLElement>('.composer')!;
	composer.focus();
	host.querySelector<HTMLButtonElement>('.send-btn')!.click();
	expect(wsMocks.sendMessage).toHaveBeenCalledOnce();

	return { composer, onAutoCreateThread, onSend };
}

function selectUnavailableModel() {
	activeModelId.set('model-unavailable');
	models.set([{
		id: 'model-unavailable',
		name: 'Unavailable model',
		size: '',
		backend: 'local',
		speed: '',
		status: 'unavailable',
	}]);
}

function setReadyCheckpointSwitch() {
	models.set([
		{
			id: 'model-unavailable',
			name: 'Unavailable source',
			size: '',
			backend: 'local',
			speed: '',
			status: 'unavailable',
		},
		{
			id: 'model-target',
			name: 'Ready target',
			size: '',
			backend: 'local',
			speed: '',
			status: 'ready',
		},
	]);
	threads.set([{
		id: 'thread-1', user_id: 'user-1', profile: 'default',
		title: 'Switching thread', model: 'model-unavailable',
		created_at: '2026-07-21T08:00:00Z', updated_at: '2026-07-21T08:00:01Z',
		pending_model: 'model-target', switch_status: 'preparing',
		switch_operation_id: 'operation-1', switch_error: null,
	}]);
}

describe('InputBar context preview ownership', () => {
	it('activates the exact preview owner only while the context sheet is open', async () => {
		activeThreadId.set('thread-1');
		const fetchMock = vi.fn(async (_input: RequestInfo | URL) => ({
			ok: true,
			json: async () => ({ preview: null }),
		}));
		vi.stubGlobal('fetch', fetchMock);
		component = mount(InputBar, {
			target: host,
			props: {
				onSend: vi.fn(),
			},
		});
		flushSync();

		const previewRequests = () => fetchMock.mock.calls.filter(([input]) =>
			String(input).includes('/context-snapshot/next-turn')
		);
		await new Promise((resolve) => setTimeout(resolve, 0));
		expect(previewRequests()).toHaveLength(0);

		host.querySelector<HTMLButtonElement>('button.context-chip')?.click();
		flushSync();

		await vi.waitFor(() => expect(previewRequests()).toHaveLength(1));

		setContextUsage('thread-1', {
			model_context_window: 1000,
			system_tokens: 0,
			tool_def_tokens: 0,
			history_tokens: 200,
			user_tokens: 0,
			total_used: 200,
			utilization: 0.2,
			was_trimmed: false,
			history_messages: 1,
			snapshot_id: 'new-boundary',
		});
		flushSync();

		await vi.waitFor(() => expect(previewRequests()).toHaveLength(2));
	});
});

describe('InputBar send and queue preparation', () => {
	it('reports composer focus and typing as explicit warm intent', async () => {
		activeThreadId.set('thread-1');
		const onTriggerWarm = vi.fn();
		component = mount(InputBar, {
			target: host,
			props: {
				onSend: vi.fn(),
				onTriggerWarm,
			},
		});
		flushSync();
		const composer = host.querySelector<HTMLElement>('.composer')!;

		composer.dispatchEvent(new FocusEvent('focus'));
		expect(onTriggerWarm).toHaveBeenCalledTimes(1);

		composer.textContent = 'hello';
		composer.dispatchEvent(new InputEvent('input', { bubbles: true }));
		expect(onTriggerWarm).toHaveBeenCalledTimes(2);
	});

	it('sends to an explicit threadId prop, not the active thread (#1949 leaf mount)', async () => {
		activeThreadId.set('active-thread');
		component = mount(InputBar, {
			target: host,
			props: {
				threadId: 'subagent-thread',
				onSend: vi.fn(),
			},
		});
		flushSync();
		await component.setText('hello subagent');

		host.querySelector<HTMLButtonElement>('.send-btn')!.click();
		await vi.waitFor(() => expect(wsMocks.sendMessage).toHaveBeenCalledOnce());

		// The worker's own selection rides the send, never the open thread's model.
		expect(wsMocks.sendMessage).toHaveBeenCalledWith(
			'subagent-thread',
			'hello subagent',
			undefined,
			[],
			undefined,
			'apply_now', [],
		);
	});

	it('sends a worker its own model once its row has loaded', async () => {
		activeThreadId.set('active-thread');
		activeModelId.set('network/other');
		models.update(rows => [...rows, {
			id: 'network/worker', name: 'Worker Model', size: '', backend: 'network', speed: '', status: 'ready',
		}]);
		_resetThreadModelTruthForTests();
		acceptThreadModelTruth('subagent-thread', 'network/worker', '2026-10-20T10:00:00.000000');
		subagentMeta.set(new Map([['subagent-thread', {
			childThreadId: 'subagent-thread', parentThreadId: 'active-thread', name: 'Worker',
			provider: 'cc', status: 'running',
		} as never]]));
		// The worker's own catalog, read on mount, lists its model.
		vi.stubGlobal('fetch', vi.fn(async () => ({
			ok: true,
			json: async () => ({ models: get(models) }),
		})));
		component = mount(InputBar, {
			target: host,
			props: { threadId: 'subagent-thread', onSend: vi.fn() },
		});
		flushSync();
		await component.setText('hello worker');

		host.querySelector<HTMLButtonElement>('.send-btn')!.click();
		await vi.waitFor(() => expect(wsMocks.sendMessage).toHaveBeenCalledOnce());

		expect(wsMocks.sendMessage).toHaveBeenCalledWith(
			'subagent-thread', 'hello worker', 'network/worker', [], undefined, 'apply_now', [],
		);
	});

	it('does not dispatch later when an absent session has no auto-create callback', async () => {
		component = mount(InputBar, {
			target: host,
			props: {
				onSend: vi.fn(),
			},
		});
		flushSync();
		await component.setText('do not send');

		host.querySelector<HTMLButtonElement>('.send-btn')!.click();
		activeThreadId.set('late-thread');
		await new Promise((resolve) => setTimeout(resolve, 0));

		expect(wsMocks.sendMessage).not.toHaveBeenCalled();
	});

	it('clears the composer after the local send and keeps its draft for recovery', async () => {
		const { composer, onAutoCreateThread, onSend } = await mountPreparedComposer({
			switchThreadOnSend: false,
		});

		expect(wsMocks.sendMessage).toHaveBeenCalledWith(
				'thread-1',
				'revised message',
				'network/model',
				[attachment],
				undefined,
				'apply_now', [],
			);
		expect(onAutoCreateThread).not.toHaveBeenCalled();
		expect(productEventMock).toHaveBeenCalledWith(
				'composer',
				'sent',
				{
					thread_id: 'thread-1',
					char_count: 15,
					attachment_count: 1,
				},
			);
		expect(component!.getInputText()).toBe('');
		expect(composer.textContent).toBe('');
		expect(host.querySelector('.attachment-chip')).toBeNull();

		expect(onSend).toHaveBeenCalledOnce();
		expect(warmToastMock).not.toHaveBeenCalled();
		expect(document.activeElement).toBe(composer);

		wsMocks.emitForTest('input_queued', {
			thread_id: 'thread-1',
			input_id: 'input-1',
			message_id: 'canonical-input-1',
			delivery_state: 'queued',
		});
		flushSync();

		expect(component!.getInputText()).toBe('');
		expect(host.querySelector<HTMLElement>('.composer')?.textContent).toBe('');
		expect(host.querySelector('.attachment-chip')).toBeNull();
	});

	it('restores the submitted draft if the server reports the correlated message failed', async () => {
		activeThreadId.set('thread-1');
		wsMocks.sendMessage.mockReturnValueOnce('input-restore-1');
		component = mount(InputBar, {
			target: host,
			props: { onSend: vi.fn() },
		});
		flushSync();
		await component.setText('do not lose this');
		host.querySelector<HTMLButtonElement>('.send-btn')!.click();

		expect(component.getInputText()).toBe('');
		wsMocks.emitForTest('message_added', {
			thread_id: 'thread-1',
			message: {
				id: 'server-input-1',
				metadata: { input_id: 'input-restore-1' },
				delivery_state: 'failed',
			},
		});
		flushSync();

		expect(component.getInputText()).toBe('do not lose this');
		expect(host.querySelector<HTMLElement>('.composer')?.textContent).toBe('do not lose this');
	});

	it('preserves a full submitted draft through a thread-switch failure without overwriting newer typing', async () => {
		const { composer } = await mountPreparedComposer();
		flushSync();
		await component!.setText('newer thread draft');
		expect(composer.textContent).toBe('newer thread draft');

		wsMocks.emitForTest('message_added', {
			thread_id: 'thread-1',
			message: {
				id: 'canonical-input-original',
				metadata: { input_id: 'input-1' },
				delivery_state: 'failed',
			},
		});
		flushSync();

		expect(component!.getInputText()).toBe('newer thread draft');
		expect(host.querySelector<HTMLElement>('.composer')?.textContent).toBe('newer thread draft');

		activeThreadId.set('thread-1');
		flushSync();
		expect(component!.getInputText()).toBe('  revised message  ');
		expect(host.querySelector('.attachment-chip')).toBeTruthy();

		host.querySelector<HTMLButtonElement>('.send-btn')!.click();
		expect(wsMocks.sendMessage).toHaveBeenLastCalledWith(
			'thread-1',
			'revised message',
			'network/model',
			[attachment],
			undefined,
			'apply_now', [],
		);
	});

	it('does not erase newer typing when a prior submission receives a durable receipt', async () => {
		activeThreadId.set('thread-1');
		wsMocks.sendMessage.mockReturnValueOnce('input-old');
		component = mount(InputBar, {
			target: host,
			props: { onSend: vi.fn() },
		});
		flushSync();
		await component.setText('old thought');
		host.querySelector<HTMLButtonElement>('.send-btn')!.click();
		await component.setText('new thought');

		wsMocks.emitForTest('input_queued', {
			thread_id: 'thread-1', input_id: 'input-old',
			message_id: 'canonical-input-old', delivery_state: 'queued',
		});
		flushSync();

		expect(component.getInputText()).toBe('new thought');
		expect(host.querySelector<HTMLElement>('.composer')?.textContent).toBe('new thought');
	});

	it('ignores a repeated click after clearing, then sends newly typed text', async () => {
		activeThreadId.set('thread-1');
		wsMocks.sendMessage
			.mockReturnValueOnce('input-first')
			.mockReturnValueOnce('input-second')
			.mockReturnValueOnce('input-third');
		component = mount(InputBar, {
			target: host,
			props: { onSend: vi.fn() },
		});
		flushSync();
		await component.setText('first thought');
		const send = host.querySelector<HTMLButtonElement>('.send-btn')!;
		send.click();
		send.click();

		expect(wsMocks.sendMessage).toHaveBeenCalledOnce();
		expect(wsMocks.sendMessage).toHaveBeenLastCalledWith(
			'thread-1', 'first thought', 'network/model', [], undefined, 'apply_now', [],
		);

		await component.setText('first thought, clarified');
		send.click();

		expect(wsMocks.sendMessage).toHaveBeenCalledTimes(2);
		expect(wsMocks.sendMessage).toHaveBeenLastCalledWith(
			'thread-1', 'first thought, clarified', 'network/model', [], undefined, 'apply_now', [],
		);
	});

	it('persists a whitespace-equivalent repeated submission as another user intent', async () => {
		activeThreadId.set('thread-1');
		component = mount(InputBar, {
			target: host,
			props: { onSend: vi.fn() },
		});
		flushSync();
		await component.setText('first thought');
		const send = host.querySelector<HTMLButtonElement>('.send-btn')!;
		send.click();

		await component.setText('  first thought  ');
		send.click();

		expect(wsMocks.sendMessage).toHaveBeenCalledTimes(2);
	});

	it('allows explicitly retyped content to send through a newly selected model', async () => {
		activeThreadId.set('thread-1');
		activeModelId.set('network/model-a');
		models.set([
			{
				id: 'network/model-a', name: 'Model A', size: '', backend: 'network',
				speed: '', status: 'ready',
			},
			{
				id: 'network/model-b', name: 'Model B', size: '', backend: 'network',
				speed: '', status: 'ready',
			},
		]);
		let inputNumber = 0;
		wsMocks.sendMessage.mockImplementation(() => `input-model-${++inputNumber}`);
		component = mount(InputBar, {
			target: host,
			props: { onSend: vi.fn() },
		});
		flushSync();
		await component.setText('first thought');
		const send = host.querySelector<HTMLButtonElement>('.send-btn')!;
		send.click();

		activeModelId.set('network/model-b');
		flushSync();
		await component.setText('first thought');
		send.click();

		expect(wsMocks.sendMessage).toHaveBeenCalledTimes(2);
		expect(wsMocks.sendMessage).toHaveBeenLastCalledWith(
			'thread-1', 'first thought', 'network/model-b', [], undefined, 'apply_now', [],
		);
	});

	it('settles a draft on its correlated durable inbox receipt', async () => {
		activeThreadId.set('thread-1');
		wsMocks.sendMessage.mockReturnValueOnce('input-queued-1');
		component = mount(InputBar, {
			target: host,
			props: { onSend: vi.fn() },
		});
		flushSync();
		await component.setText('durably queued');
		host.querySelector<HTMLButtonElement>('.send-btn')!.click();
		wsMocks.emitForTest('input_queued', {
			thread_id: 'thread-1',
			input_id: 'input-queued-1',
			message_id: 'server-input-2',
			delivery_state: 'queued',
		});
		wsMocks.emitForTest('error', {
			thread_id: 'thread-1', input_id: 'input-queued-1',
			message: 'late duplicate error',
		});
		flushSync();

		expect(component.getInputText()).toBe('');
	});

	it('does not restore a draft when an authoritative queued error wins the socket race', async () => {
		activeThreadId.set('thread-1');
		wsMocks.sendMessage.mockReturnValueOnce('input-queued-race-1');
		component = mount(InputBar, {
			target: host,
			props: { onSend: vi.fn() },
		});
		flushSync();
		await component.setText('durably queued despite preflight');
		host.querySelector<HTMLButtonElement>('.send-btn')!.click();

		// The direct origin error may arrive before the fire-and-forget
		// cross-device `message_added`/`input_queued` broadcast. Its public
		// delivery state is authoritative, so it must settle—not restore—the
		// composer draft.
		wsMocks.emitForTest('error', {
			thread_id: 'thread-1', input_id: 'input-queued-race-1',
			message_id: 'server-input-race-1',
			message: 'Provider preflight is temporarily unavailable',
			delivery_state: 'queued', delivery_error: null, delivery_revision: 1,
		});
		wsMocks.emitForTest('input_queued', {
			thread_id: 'thread-1', input_id: 'input-queued-race-1',
			message_id: 'server-input-race-1',
			delivery_state: 'queued', delivery_error: null, delivery_revision: 1,
		});
		flushSync();

		expect(component.getInputText()).toBe('');
	});
});

describe('InputBar while CORSIA is restarting', () => {
	it('preserves the draft and blocks Enter and paste until CORSIA returns', async () => {
		activeThreadId.set('thread-1');
		const onSend = vi.fn();
		component = mount(InputBar, { target: host, props: { onSend } });
		flushSync();
		await component.setText('saved before restart');
		let composer = host.querySelector<HTMLElement>('.composer')!;

		updateStatus.set({
			branch: 'dev', current_sha: 'a', remote_sha: 'b', commits_behind: 1,
			available: true, checked_at: 1, error: null,
			operation: { operation_id: 'update-1', kind: 'update', status: 'in_flight', phase: 'restarting' },
		});
		flushSync();
		composer = host.querySelector<HTMLElement>('.composer')!;

		expect(composer.getAttribute('contenteditable')).toBe('false');
		expect(composer.getAttribute('aria-disabled')).toBe('true');
		const enter = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
		const paste = new Event('paste', { bubbles: true, cancelable: true });
		Object.defineProperty(paste, 'clipboardData', {
			value: { files: [], getData: () => 'new text' },
		});
		composer.dispatchEvent(enter);
		composer.dispatchEvent(paste);
		await Promise.resolve();

		expect(paste.defaultPrevented).toBe(true);
		expect(wsMocks.sendMessage).not.toHaveBeenCalled();
		expect(onSend).not.toHaveBeenCalled();
		expect(component.getInputText()).toBe('saved before restart');
		expect(composer.textContent).toBe('saved before restart');
	});
});

describe('InputBar model readiness at the action seam', () => {
	it.each([
		'enter',
		'modified-enter',
		'set-and-send',
		'streaming-button',
	] as const)('keeps typed input intact when %s has no ready destination', async (action) => {
		activeThreadId.set('thread-1');
		selectUnavailableModel();
		if (action === 'streaming-button') {
			applyLifecycle('thread-1', { turnId: 'turn-1', turnOrder: 1 });
		}
		const onSend = vi.fn();
		component = mount(InputBar, {
			target: host,
			props: {
				onSend,
			},
		});
		flushSync();
		await component.setText('keep this thought');

		const composer = host.querySelector<HTMLElement>('.composer')!;
		if (action === 'enter') {
			composer.dispatchEvent(new KeyboardEvent('keydown', {
				key: 'Enter', bubbles: true,
			}));
		} else if (action === 'modified-enter') {
			composer.dispatchEvent(new KeyboardEvent('keydown', {
				key: 'Enter', ctrlKey: true, bubbles: true,
			}));
		} else if (action === 'set-and-send') {
			await component.setAndSend('keep this thought');
		} else {
			host.querySelector<HTMLButtonElement>('.send-btn')!.click();
		}
		await Promise.resolve();

		expect(wsMocks.sendMessage).not.toHaveBeenCalled();
		expect(onSend).not.toHaveBeenCalled();
		expect(component.getInputText()).toBe('keep this thought');
		expect(composer.textContent).toBe('keep this thought');
	});

	it('sends an automatic message without changing the saved draft or its attachment', async () => {
		activeThreadId.set('thread-1');
		threads.set([{
			id: 'thread-1', user_id: 'user-1', profile: 'default',
			title: 'CORSIA', model: 'network/model',
			created_at: '2026-09-29T08:00:00Z', updated_at: '2026-09-29T08:00:01Z',
		}]);
		component = mount(InputBar, { target: host, props: { onSend: vi.fn() } });
		flushSync();
		await component.setText('Keep this draft');
		const fileInput = host.querySelector<HTMLInputElement>('input[type="file"]')!;
		Object.defineProperty(fileInput, 'files', {
			configurable: true,
			value: [new File(['notes'], attachment.filename, { type: attachment.mime_type })],
		});
		fileInput.dispatchEvent(new Event('change', { bubbles: true }));
		await vi.waitFor(() => expect(host.querySelector('.attachment-chip')).not.toBeNull());

		await component.setAndSend('Connect a Google account for all my projects', true);

		expect(wsMocks.sendMessage).toHaveBeenCalledWith(
			'thread-1', 'Connect a Google account for all my projects', 'network/model', [], undefined, 'apply_now', [],
		);
		expect(component.getInputText()).toBe('Keep this draft');
		expect(host.querySelector('.attachment-chip')).not.toBeNull();
		expect(component.getDrafts().get('thread-1')).toMatchObject({ content: 'Keep this draft', attachments: [attachment] });
	});

	it('waits for a new thread before restoring the draft around an automatic send', async () => {
		activeThreadId.set('thread-before-automatic-send');
		let resolveThread!: () => void;
		const onAutoCreateThread = vi.fn(() => new Promise<void>(resolve => {
			resolveThread = () => {
				activeThreadId.set('thread-created-for-automatic-send');
				resolve();
			};
		}));
		component = mount(InputBar, { target: host, props: { onSend: vi.fn(), onAutoCreateThread } });
		flushSync();
		await component.setText('Keep this draft');
		const fileInput = host.querySelector<HTMLInputElement>('input[type="file"]')!;
		Object.defineProperty(fileInput, 'files', {
			configurable: true,
			value: [new File(['notes'], attachment.filename, { type: attachment.mime_type })],
		});
		fileInput.dispatchEvent(new Event('change', { bubbles: true }));
		await vi.waitFor(() => expect(host.querySelector('.attachment-chip')).not.toBeNull());
		activeThreadId.set(null);
		flushSync();

		const automaticSend = component.setAndSend('Connect a Google account for all my projects', true);
		await vi.waitFor(() => expect(onAutoCreateThread).toHaveBeenCalledOnce());
		resolveThread();
		await automaticSend;

		expect(wsMocks.sendMessage).toHaveBeenCalledWith(
			'thread-created-for-automatic-send', 'Connect a Google account for all my projects', 'network/model', [], undefined, 'apply_now', [],
		);
		expect(component.getInputText()).toBe('Keep this draft');
		expect(host.querySelector('.attachment-chip')).not.toBeNull();
		expect(component.getDrafts().get('thread-before-automatic-send')).toMatchObject({
			content: 'Keep this draft', attachments: [attachment],
		});
	});

	describe('an automatic send that fails keeps the draft and reports how it failed', () => {
		async function mountWithDraft() {
			activeThreadId.set('thread-1');
			threads.set([{
				id: 'thread-1', user_id: 'user-1', profile: 'default',
				title: 'CORSIA', model: 'network/model',
				created_at: '2026-09-29T08:00:00Z', updated_at: '2026-09-29T08:00:01Z',
			}]);
			const onSend = vi.fn();
			component = mount(InputBar, { target: host, props: { onSend } });
			flushSync();
			await component.setText('Keep this draft');
			const fileInput = host.querySelector<HTMLInputElement>('input[type="file"]')!;
			Object.defineProperty(fileInput, 'files', {
				configurable: true,
				value: [new File(['notes'], attachment.filename, { type: attachment.mime_type })],
			});
			fileInput.dispatchEvent(new Event('change', { bubbles: true }));
			await vi.waitFor(() => expect(host.querySelector('.attachment-chip')).not.toBeNull());
			return onSend;
		}

		function expectDraftKept(): void {
			expect(component!.getInputText()).toBe('Keep this draft');
			expect(host.querySelector<HTMLElement>('.composer')!.textContent).toBe('Keep this draft');
			expect(host.querySelector('.attachment-chip')).not.toBeNull();
		}

		it('returns not_sent with no toast of its own when the model cannot take it', async () => {
			const onSend = await mountWithDraft();
			selectUnavailableModel();
			flushSync();
			const before = get(toasts).length;

			const outcome = await component!.setAndSend('Connect a Google account for all my projects', true);

			expect(outcome).toBe('not_sent');
			expect(wsMocks.sendMessage).not.toHaveBeenCalled();
			expect(onSend).not.toHaveBeenCalled();
			expect(get(toasts)).toHaveLength(before);
			expectDraftKept();
		});

		it('returns notified after its own connection-lost toast', async () => {
			const onSend = await mountWithDraft();
			wsMocks.sendMessage.mockReturnValueOnce(null);
			const before = get(toasts).length;

			const outcome = await component!.setAndSend('Connect a Google account for all my projects', true);

			expect(outcome).toBe('notified');
			expect(onSend).not.toHaveBeenCalled();
			expect(get(toasts).slice(before)).toEqual([
				expect.objectContaining({ message: 'Connection lost before CORSIA could accept this message.', type: 'error' }),
			]);
			expectDraftKept();
		});

		it('restores the draft and attachment when sending rejects', async () => {
			await mountWithDraft();
			wsMocks.sendMessage.mockImplementationOnce(() => { throw new Error('send failed'); });

			await expect(component!.setAndSend('Connect a Google account for all my projects', true)).rejects.toThrow('send failed');

			expectDraftKept();
		});
	});

	it('sends from a thread with no model yet, for the server to hold until one is chosen', async () => {
		activeThreadId.set('thread-1');
		activeModelId.set(null);
		threads.set([{
			id: 'thread-1', user_id: 'user-1', profile: 'default',
			title: 'No model yet', model: null,
			created_at: '2026-09-29T08:00:00Z', updated_at: '2026-09-29T08:00:01Z',
		}]);
		const onSend = vi.fn();
		component = mount(InputBar, {
			target: host,
			props: { onSend },
		});
		flushSync();

		await component.setAndSend('hold this for a model');
		await vi.waitFor(() => expect(wsMocks.sendMessage).toHaveBeenCalledOnce());

		expect(wsMocks.sendMessage).toHaveBeenCalledWith(
			'thread-1', 'hold this for a model', undefined, [], undefined, 'apply_now', [],
		);
		expect(component.getInputText()).toBe('');
	});

	it('allows a typed input to enter the durable cutover queue when the destination is ready', async () => {
		activeThreadId.set('thread-1');
		activeModelId.set('model-unavailable');
		setReadyCheckpointSwitch();
		const onSend = vi.fn();
		component = mount(InputBar, {
			target: host,
			props: {
				onSend,
			},
		});
		flushSync();

		await component.setAndSend('queue after checkpoint');
		await vi.waitFor(() => expect(wsMocks.sendMessage).toHaveBeenCalledOnce());

		expect(wsMocks.sendMessage).toHaveBeenCalledWith(
			'thread-1',
			'queue after checkpoint',
			'model-unavailable',
			[],
			undefined,
			'apply_now', [],
		);
		expect(onSend).toHaveBeenCalledOnce();
		expect(component.getInputText()).toBe('');
	});

	it('queues from a held Send while ordinary Send remains apply_now', async () => {
		activeThreadId.set('thread-1');
		component = mount(InputBar, {
			target: host,
			props: {
				onSend: vi.fn(),
			},
		});
		flushSync();
		await component.setText('hold this');
		const send = host.querySelector<HTMLButtonElement>('.send-btn')!;
		expect(host.querySelector('.queue-btn')).toBeNull();

		vi.useFakeTimers();
		// A tap is a tap: below the arm delay nothing queues.
		send.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
		vi.advanceTimersByTime(ARM_MS - 1);
		send.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }));
		expect(wsMocks.sendMessage).not.toHaveBeenCalled();

		// Travel means the finger is dragging or scrolling, not holding.
		send.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, clientX: 0, clientY: 0 }));
		send.dispatchEvent(new PointerEvent('pointermove', { bubbles: true, clientX: 0, clientY: 40 }));
		vi.advanceTimersByTime(ARM_MS);
		flushSync();
		expect(send.classList.contains('queue-armed')).toBe(false);
		send.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }));
		expect(wsMocks.sendMessage).not.toHaveBeenCalled();

		send.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
		vi.advanceTimersByTime(ARM_MS);
		flushSync();
		expect(send.classList.contains('queue-armed')).toBe(true);
		expect(send.getAttribute('aria-label')).toBe('Release to queue after the current turn');
		send.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }));
		send.click();
		vi.useRealTimers();

		expect(wsMocks.sendMessage).toHaveBeenCalledTimes(1);
		expect(wsMocks.sendMessage).toHaveBeenCalledWith(
			'thread-1', 'hold this', 'network/model', [], undefined, 'queue_after_turn', [],
		);

		await component.setText('ordinary send');
		send.click();
		expect(wsMocks.sendMessage).toHaveBeenLastCalledWith(
			'thread-1', 'ordinary send', 'network/model', [], undefined, 'apply_now', [],
		);
	});

	it('shows the hold landing before the user lets go', async () => {
		// USER-REPORTS #1: the hold gave no feedback until release, so a hold
		// that had armed and a hold that had not looked identical.
		activeThreadId.set('thread-1');
		component = mount(InputBar, {
			target: host,
			props: { onSend: vi.fn() },
		});
		flushSync();
		await component.setText('hold this');
		const send = host.querySelector<HTMLButtonElement>('.send-btn')!;

		vi.useFakeTimers();
		send.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
		flushSync();
		expect(send.classList.contains('queue-holding')).toBe(true);
		expect(send.classList.contains('queue-armed')).toBe(false);

		vi.advanceTimersByTime(ARM_MS);
		flushSync();
		expect(send.classList.contains('queue-armed')).toBe(true);
		expect(vibrate).toHaveBeenCalled();

		send.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }));
		flushSync();
		vi.useRealTimers();
		expect(send.classList.contains('queue-holding')).toBe(false);
		expect(send.classList.contains('queue-armed')).toBe(false);
	});

	it('clears an armed hold when the window blurs or the page hides, with nothing sent', async () => {
		// The queue glyph must never outlive the gesture: leaving the page
		// mid-hold ends it, and no pointerup may ever arrive.
		activeThreadId.set('thread-1');
		component = mount(InputBar, { target: host, props: { onSend: vi.fn() } });
		flushSync();
		await component.setText('hold this');
		const send = host.querySelector<HTMLButtonElement>('.send-btn')!;
		const hide = () => {
			Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' });
			document.dispatchEvent(new Event('visibilitychange'));
		};
		const ends = [() => window.dispatchEvent(new Event('blur')), hide];

		vi.useFakeTimers();
		for (const end of ends) {
			send.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
			vi.advanceTimersByTime(ARM_MS);
			flushSync();
			expect(send.classList.contains('queue-armed')).toBe(true);
			end();
			flushSync();
			expect(send.classList.contains('queue-armed')).toBe(false);
			expect(send.classList.contains('queue-holding')).toBe(false);
		}
		Reflect.deleteProperty(document, 'visibilityState');
		vi.useRealTimers();
		expect(wsMocks.sendMessage).not.toHaveBeenCalled();
	});

	it('finishes an armed hold from document pointerup when the button misses the release', async () => {
		activeThreadId.set('thread-1');
		component = mount(InputBar, { target: host, props: { onSend: vi.fn() } });
		flushSync();
		await component.setText('hold this');
		const send = host.querySelector<HTMLButtonElement>('.send-btn')!;

		vi.useFakeTimers();
		send.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerId: 7 }));
		vi.advanceTimersByTime(ARM_MS);
		flushSync();
		expect(send.classList.contains('queue-armed')).toBe(true);

		document.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, pointerId: 7 }));
		flushSync();
		vi.useRealTimers();
		expect(send.classList.contains('queue-armed')).toBe(false);
		expect(send.classList.contains('queue-holding')).toBe(false);
		expect(wsMocks.sendMessage).toHaveBeenCalledTimes(1);
		expect(wsMocks.sendMessage).toHaveBeenCalledWith(
			'thread-1', 'hold this', 'network/model', [], undefined, 'queue_after_turn', [],
		);
	});

	it('clears a hold when the send button becomes disabled', async () => {
		activeThreadId.set('thread-1');
		component = mount(InputBar, { target: host, props: { onSend: vi.fn() } });
		flushSync();
		await component.setText('hold this');
		const send = host.querySelector<HTMLButtonElement>('.send-btn')!;

		vi.useFakeTimers();
		send.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerId: 8 }));
		vi.advanceTimersByTime(ARM_MS);
		connectionState.set('disconnected');
		flushSync();
		vi.useRealTimers();
		expect(send.disabled).toBe(true);
		expect(send.classList.contains('queue-armed')).toBe(false);
		expect(send.classList.contains('queue-holding')).toBe(false);
		expect(wsMocks.sendMessage).not.toHaveBeenCalled();
	});

	it('clears a hold when its input is emptied', async () => {
		activeThreadId.set('thread-1');
		component = mount(InputBar, { target: host, props: { onSend: vi.fn() } });
		flushSync();
		await component.setText('hold this');
		const send = host.querySelector<HTMLButtonElement>('.send-btn')!;

		vi.useFakeTimers();
		send.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerId: 9 }));
		vi.advanceTimersByTime(ARM_MS);
		await component.setText('');
		flushSync();
		vi.useRealTimers();
		expect(send.classList.contains('queue-armed')).toBe(false);
		expect(send.classList.contains('queue-holding')).toBe(false);
		expect(wsMocks.sendMessage).not.toHaveBeenCalled();
	});

	it('clears a hold when its thread changes', async () => {
		activeThreadId.set('thread-1');
		component = mount(InputBar, { target: host, props: { onSend: vi.fn() } });
		flushSync();
		await component.setText('hold this');
		const send = host.querySelector<HTMLButtonElement>('.send-btn')!;

		vi.useFakeTimers();
		send.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerId: 10 }));
		vi.advanceTimersByTime(ARM_MS);
		activeThreadId.set('thread-2');
		flushSync();
		vi.useRealTimers();
		expect(send.classList.contains('queue-armed')).toBe(false);
		expect(send.classList.contains('queue-holding')).toBe(false);
		expect(wsMocks.sendMessage).not.toHaveBeenCalled();

		// The finger that was holding lifts over the other thread's draft: no tap.
		await component.setText('other draft');
		document.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, pointerId: 10 }));
		send.dispatchEvent(new PointerEvent('lostpointercapture', { bubbles: true, pointerId: 10 }));
		send.click();
		expect(wsMocks.sendMessage).not.toHaveBeenCalled();
	});

	it('does not stop the turn when a hold whose input was emptied is released', async () => {
		activeThreadId.set('thread-1');
		applyLifecycle('thread-1', { turnId: 'turn-1', turnOrder: 1 });
		component = mount(InputBar, { target: host, props: { onSend: vi.fn() } });
		flushSync();
		await component.setText('hold this');
		const send = host.querySelector<HTMLButtonElement>('.send-btn')!;

		vi.useFakeTimers();
		send.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerId: 13 }));
		vi.advanceTimersByTime(ARM_MS);
		await component.setText('');
		flushSync();
		vi.useRealTimers();
		expect(send.classList.contains('queue-armed')).toBe(false);

		document.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, pointerId: 13 }));
		send.dispatchEvent(new PointerEvent('lostpointercapture', { bubbles: true, pointerId: 13 }));
		send.click();
		expect(wsMocks.stopTurn).not.toHaveBeenCalled();
		expect(wsMocks.sendMessage).not.toHaveBeenCalled();

		// The next deliberate tap still works.
		send.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerId: 14 }));
		document.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, pointerId: 14 }));
		send.click();
		expect(wsMocks.stopTurn).toHaveBeenCalledWith('thread-1');
	});

	it('sends an ordinary tap once after document-level release handling', async () => {
		activeThreadId.set('thread-1');
		component = mount(InputBar, { target: host, props: { onSend: vi.fn() } });
		flushSync();
		await component.setText('ordinary send');
		const send = host.querySelector<HTMLButtonElement>('.send-btn')!;

		send.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerId: 11 }));
		document.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, pointerId: 11 }));
		send.click();

		expect(wsMocks.sendMessage).toHaveBeenCalledTimes(1);
		expect(wsMocks.sendMessage).toHaveBeenCalledWith(
			'thread-1', 'ordinary send', 'network/model', [], undefined, 'apply_now', [],
		);
	});

	it('queues once when the platform cancels an armed hold on the document', async () => {
		activeThreadId.set('thread-1');
		component = mount(InputBar, { target: host, props: { onSend: vi.fn() } });
		flushSync();
		await component.setText('hold this');
		const send = host.querySelector<HTMLButtonElement>('.send-btn')!;

		vi.useFakeTimers();
		send.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerId: 12 }));
		vi.advanceTimersByTime(ARM_MS);
		flushSync();
		document.dispatchEvent(new PointerEvent('pointercancel', { bubbles: true, pointerId: 12 }));
		send.dispatchEvent(new PointerEvent('lostpointercapture', { bubbles: true, pointerId: 12 }));
		flushSync();
		vi.useRealTimers();
		expect(send.classList.contains('queue-armed')).toBe(false);
		expect(send.classList.contains('queue-holding')).toBe(false);
		expect(wsMocks.sendMessage).toHaveBeenCalledTimes(1);
		expect(wsMocks.sendMessage).toHaveBeenCalledWith(
			'thread-1', 'hold this', 'network/model', [], undefined, 'queue_after_turn', [],
		);
	});

	it('sends nothing when the platform cancels a hold before it arms', async () => {
		activeThreadId.set('thread-1');
		component = mount(InputBar, { target: host, props: { onSend: vi.fn() } });
		flushSync();
		await component.setText('hold this');
		const send = host.querySelector<HTMLButtonElement>('.send-btn')!;

		vi.useFakeTimers();
		send.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerId: 13 }));
		vi.advanceTimersByTime(ARM_MS - 100);
		document.dispatchEvent(new PointerEvent('pointercancel', { bubbles: true, pointerId: 13 }));
		vi.advanceTimersByTime(200);
		flushSync();
		vi.useRealTimers();
		expect(send.classList.contains('queue-armed')).toBe(false);
		expect(send.classList.contains('queue-holding')).toBe(false);
		expect(wsMocks.sendMessage).not.toHaveBeenCalled();
	});

	it('treats lost pointer capture like pointercancel and queues once', async () => {
		// Capture lost is the platform taking the touch: past the arm delay the
		// gesture is made, as for pointercancel; before it, nothing is sent. The
		// lostpointercapture that follows a pointercancel or pointerup is a no-op.
		activeThreadId.set('thread-1');
		component = mount(InputBar, { target: host, props: { onSend: vi.fn() } });
		flushSync();
		await component.setText('hold this');
		const send = host.querySelector<HTMLButtonElement>('.send-btn')!;
		const lose = () => send.dispatchEvent(new PointerEvent('lostpointercapture', { bubbles: true }));

		vi.useFakeTimers();
		send.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
		vi.advanceTimersByTime(ARM_MS - 100);
		lose();
		flushSync();
		expect(send.classList.contains('queue-holding')).toBe(false);
		expect(wsMocks.sendMessage).not.toHaveBeenCalled();

		send.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
		vi.advanceTimersByTime(ARM_MS);
		send.dispatchEvent(new PointerEvent('pointercancel', { bubbles: true }));
		lose();
		flushSync();
		vi.useRealTimers();
		expect(send.classList.contains('queue-armed')).toBe(false);
		expect(wsMocks.sendMessage).toHaveBeenCalledTimes(1);
		expect(wsMocks.sendMessage).toHaveBeenCalledWith(
			'thread-1', 'hold this', 'network/model', [], undefined, 'queue_after_turn', [],
		);
	});

	it('still queues when iOS takes the touch away after the hold has landed', async () => {
		// USER-REPORTS #1 root cause: Safari claims a long-press and fires
		// `pointercancel`, which disarmed the hold — so a deliberate queue was
		// delivered as a steer. Past the arm delay the gesture is already made.
		activeThreadId.set('thread-1');
		component = mount(InputBar, {
			target: host,
			props: { onSend: vi.fn() },
		});
		flushSync();
		await component.setText('hold this');
		const send = host.querySelector<HTMLButtonElement>('.send-btn')!;

		vi.useFakeTimers();
		send.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
		vi.advanceTimersByTime(ARM_MS);
		send.dispatchEvent(new PointerEvent('pointercancel', { bubbles: true }));
		vi.useRealTimers();

		expect(wsMocks.sendMessage).toHaveBeenCalledWith(
			'thread-1', 'hold this', 'network/model', [], undefined, 'queue_after_turn', [],
		);
	});

	it('does not let an aborted hold swallow the next send', async () => {
		// A cancelled gesture emits no `click`, so the old boolean suppression
		// outlived its own gesture and ate the user's NEXT tap — a Send that
		// silently did nothing.
		activeThreadId.set('thread-1');
		component = mount(InputBar, {
			target: host,
			props: { onSend: vi.fn() },
		});
		flushSync();
		await component.setText('hold this');
		const send = host.querySelector<HTMLButtonElement>('.send-btn')!;

		vi.useFakeTimers();
		send.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
		vi.advanceTimersByTime(ARM_MS);
		send.dispatchEvent(new PointerEvent('pointercancel', { bubbles: true }));
		expect(wsMocks.sendMessage).toHaveBeenCalledTimes(1);

		// The suppression window is bounded, so a later deliberate tap sends.
		vi.advanceTimersByTime(2000);
		await component.setText('ordinary send');
		send.click();
		vi.useRealTimers();

		expect(wsMocks.sendMessage).toHaveBeenLastCalledWith(
			'thread-1', 'ordinary send', 'network/model', [], undefined, 'apply_now', [],
		);
	});

	it('cancels a hold that never reached the arm delay when iOS takes the touch', async () => {
		activeThreadId.set('thread-1');
		component = mount(InputBar, {
			target: host,
			props: { onSend: vi.fn() },
		});
		flushSync();
		await component.setText('hold this');
		const send = host.querySelector<HTMLButtonElement>('.send-btn')!;

		vi.useFakeTimers();
		send.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
		vi.advanceTimersByTime(ARM_MS - 100);
		send.dispatchEvent(new PointerEvent('pointercancel', { bubbles: true }));
		vi.useRealTimers();

		expect(wsMocks.sendMessage).not.toHaveBeenCalled();
	});

	it.each([
		['Control', { ctrlKey: true }],
		['Command', { metaKey: true }],
	] as const)('queues with %s+Enter without rendering a separate Queue control', async (_label, modifier) => {
		activeThreadId.set('thread-1');
		component = mount(InputBar, {
			target: host,
			props: {
				onSend: vi.fn(),
			},
		});
		flushSync();
		await component.setText('keyboard queue');

		expect(host.querySelector('.queue-btn')).toBeNull();
		host.querySelector<HTMLElement>('.composer')!.dispatchEvent(new KeyboardEvent('keydown', {
			key: 'Enter',
			bubbles: true,
			...modifier,
		}));
		await Promise.resolve();

		expect(wsMocks.sendMessage).toHaveBeenCalledWith(
			'thread-1', 'keyboard queue', 'network/model', [], undefined, 'queue_after_turn', [],
		);
	});

	it('still stops an active turn with an empty composer when the model is unavailable', () => {
		activeThreadId.set('thread-1');
		selectUnavailableModel();
		applyLifecycle('thread-1', { turnId: 'turn-1', turnOrder: 1 });
		component = mount(InputBar, {
			target: host,
			props: {
				onSend: vi.fn(),
			},
		});
		flushSync();

		const stop = host.querySelector<HTMLButtonElement>('.send-btn')!;
		expect(stop.disabled).toBe(false);
		stop.click();

		expect(wsMocks.stopTurn).toHaveBeenCalledWith('thread-1');
		expect(wsMocks.sendMessage).not.toHaveBeenCalled();
	});

	it('surfaces Stop receipts only for the active session', () => {
		activeThreadId.set('thread-1');
		component = mount(InputBar, {
			target: host,
			props: { onSend: vi.fn() },
		});
		flushSync();
		const before = get(toasts).length;

		wsMocks.emitForTest('stop_receipt', {
			thread_id: 'thread-foreign', outcome: 'stale_active', turn_id: 'turn-old',
		});
		expect(get(toasts)).toHaveLength(before);

		wsMocks.emitForTest('stop_receipt', {
			thread_id: 'thread-1', outcome: 'stopped', turn_id: 'turn-1',
		});
		expect(get(toasts).at(-1)).toMatchObject({ message: 'Turn stopped', type: 'success' });

		wsMocks.emitForTest('stop_receipt', {
			thread_id: 'thread-1', outcome: 'no_active_turn',
		});
		expect(get(toasts).at(-1)).toMatchObject({
			message: 'Nothing was running to stop', type: 'info',
		});
	});

	it('acknowledges a Stop while its turn is still live, and resolves when it ends', () => {
		// USER-REPORTS #6b: the turn terminalized and the UI kept offering Stop,
		// so the user could not tell a working Stop from a broken one.
		activeThreadId.set('thread-1');
		applyLifecycle('thread-1', { turnId: 'turn-1', turnOrder: 1 });
		component = mount(InputBar, {
			target: host,
			props: { onSend: vi.fn() },
		});
		flushSync();

		const stop = host.querySelector<HTMLButtonElement>('.send-btn')!;
		expect(stop.getAttribute('aria-label')).toBe('Stop turn');

		stopRequestedTurns.set(new Map([['thread-1', 'turn-1']]));
		flushSync();
		expect(stop.getAttribute('aria-label')).toBe('Stopping this turn');
		expect(stop.classList.contains('stopping')).toBe(true);

		applyTerminal('thread-1', { turnId: 'turn-1', turnOrder: 1 }, false);
		flushSync();
		expect(host.querySelector('.send-btn.stop-btn')).toBeNull();
	});
});

// The chip stack above the composer renders the QUEUE and nothing else, read
// straight off the message store — never a side store, which is what made a
// failed snapshot decode erase the user's text from the screen entirely. A
// message waiting on a checkpoint is an ordinary pending row in the transcript.
// The stack is covered by InputBarQueuedInputs.test.ts; what stays out of the
// canvas is covered by MessageList.pending-delivery.test.ts.
