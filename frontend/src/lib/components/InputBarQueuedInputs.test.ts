import { get } from 'svelte/store';
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const wsMocks = vi.hoisted(() => ({
	sendMessage: vi.fn(),
	stopTurn: vi.fn(),
	on: vi.fn(() => () => {}),
}));

vi.mock('$lib/ws', () => wsMocks);
vi.mock('$lib/product_events', () => ({ emitProductEvent: vi.fn() }));

import InputBar from './InputBar.svelte';
import { token } from '$lib/stores/auth';
import { activeModelId, models } from '$lib/stores/models';
import { activeThreadId, messages, type Message } from '$lib/stores/threads';
import { _resetTurnStateForTests } from '$lib/ws/turnState';

const THREAD_ID = 'thread-1';

let host: HTMLElement;
let component: Record<string, any> | null = null;

function queuedInput(overrides: Partial<Message> = {}): Message {
	return {
		id: 'queued-1', thread_id: THREAD_ID, role: 'user',
		content: 'first in line', created_at: '2026-07-28T12:00:00Z',
		metadata: { input_id: 'q1', delivery_intent: 'queue_after_turn' },
		delivery_state: 'queued', delivery_error: null, delivery_revision: 1,
		...overrides,
	} as Message;
}

function renderInputBar(): void {
	component = mount(InputBar, {
		target: host,
		props: { onSend: vi.fn() },
	});
	flushSync();
}

function chipTexts(): string[] {
	return [...host.querySelectorAll<HTMLElement>('.queued-chip .queued-text')]
		.map((el) => el.textContent?.trim() ?? '');
}

beforeEach(() => {
	_resetTurnStateForTests();
	token.set('token');
	activeThreadId.set(THREAD_ID);
	activeModelId.set('network/model');
	models.set([{
		id: 'network/model', name: 'Network Model', size: '',
		backend: 'network', speed: '', status: 'ready',
	}]);
	messages.set([]);
	vi.clearAllMocks();
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.unstubAllGlobals();
	_resetTurnStateForTests();
	token.set(null);
	activeThreadId.set(null);
	activeModelId.set(null);
	models.set([]);
	messages.set([]);
});

describe('the queue pinned to the top of the input box', () => {
	it('shows queued inputs above the composer field, in FIFO order', () => {
		messages.set([
			{
				id: 'delivered-1', thread_id: THREAD_ID, role: 'user',
				content: 'already sent', created_at: '2026-07-28T11:59:00Z',
				delivery_state: 'delivered', seq: 10,
			} as Message,
			{
				id: 'steer-1', thread_id: THREAD_ID, role: 'user',
				content: 'apply this now', created_at: '2026-07-28T11:59:30Z',
				metadata: { input_id: 's1' }, delivery_state: 'queued', delivery_revision: 1,
			} as Message,
			queuedInput(),
			queuedInput({
				id: 'queued-2', content: 'second in line',
				created_at: '2026-07-28T12:00:01Z',
				metadata: { input_id: 'q2', delivery_intent: 'queue_after_turn' },
			}),
		]);

		renderInputBar();

		// Only the two explicit queues are here: a delivered message belongs to
		// the transcript, and an apply-now steer is about to be sent, so it stays
		// in the canvas where "about to happen" is shown.
		expect(chipTexts()).toEqual(['first in line', 'second in line']);
		expect([...host.querySelectorAll('.queued-position')].map((el) => el.textContent?.trim()))
			.toEqual(['1', '2']);

		const stack = host.querySelector<HTMLElement>('.queued-stack')!;
		const composerChip = host.querySelector<HTMLElement>('.input-chip')!;
		expect(stack.compareDocumentPosition(composerChip) & Node.DOCUMENT_POSITION_FOLLOWING)
			.toBe(Node.DOCUMENT_POSITION_FOLLOWING);
	});

	it('carries no place in line when only one input is waiting', () => {
		messages.set([queuedInput()]);

		renderInputBar();

		expect(chipTexts()).toEqual(['first in line']);
		// A lone "1" is decoration, not information.
		expect(host.querySelector('.queued-position')).toBeNull();
	});

	it('renders nothing at all when nothing is queued', () => {
		messages.set([{
			id: 'delivered-1', thread_id: THREAD_ID, role: 'user',
			content: 'already sent', created_at: '2026-07-28T11:59:00Z',
			delivery_state: 'delivered', seq: 10,
		} as Message]);

		renderInputBar();

		expect(host.querySelector('.queued-stack')).toBeNull();
	});

	it('removes a queued input through the one delivery-action endpoint', async () => {
		const cancelUrl = '/api/threads/thread-1/messages/queued-1/cancel?token=token';
		const fetchMock = vi.fn(async (input: RequestInfo | URL) => ({
			ok: true,
			json: async () => String(input) === cancelUrl
				? {
					thread_id: THREAD_ID,
					message: {
						id: 'queued-1', thread_id: THREAD_ID, role: 'user',
						content: 'first in line', created_at: '2026-07-28T12:00:00Z',
						metadata: { input_id: 'q1', delivery_intent: 'queue_after_turn' },
						delivery_state: 'cancelled', delivery_error: 'cancelled_by_user',
						delivery_revision: 2, seq: 11,
					},
				}
				: { preview: null },
		}));
		vi.stubGlobal('fetch', fetchMock);
		messages.set([queuedInput()]);

		renderInputBar();

		const remove = host.querySelector<HTMLButtonElement>('.queued-chip .queued-remove')!;
		expect(remove.disabled).toBe(false);
		expect(remove.getAttribute('aria-label')).toBe('Remove queued message: first in line');
		remove.click();

		await vi.waitFor(() => {
			expect(fetchMock.mock.calls.map((call) => String(call[0]))).toContain(cancelUrl);
		});
		// The canonical row the server returned is adopted, so the chip leaves the
		// composer for exactly the reason it stopped waiting.
		await vi.waitFor(() => {
			flushSync();
			expect(host.querySelector('.queued-stack')).toBeNull();
		});
		expect(get(messages)[0].delivery_state).toBe('cancelled');
	});

	it('offers no removal until the queue is durable enough to remove', () => {
		// Between the send and its receipt there is no row on the server to
		// address. The control is visibly disabled rather than absent, so it does
		// not appear under a thumb already moving toward it.
		messages.set([queuedInput({
			_optimistic: true,
			delivery_state: undefined,
			delivery_error: undefined,
			delivery_revision: undefined,
		})]);

		renderInputBar();

		expect(chipTexts()).toEqual(['first in line']);
		expect(host.querySelector<HTMLButtonElement>('.queued-remove')!.disabled).toBe(true);
	});

	it('lets go of a queued input the moment it is delivered', () => {
		messages.set([queuedInput()]);
		renderInputBar();
		expect(host.querySelector('.queued-stack')).not.toBeNull();

		messages.set([queuedInput({
			delivery_state: 'delivered', delivery_error: null,
			delivery_revision: 2, seq: 41,
		})]);
		flushSync();

		// It is in the transcript now — MessageList owns it from here.
		expect(host.querySelector('.queued-stack')).toBeNull();
	});

	it('says in the row that a queued input waits for the model once the limit has stopped its turn', () => {
		const limited = {
			id: 'codex/gpt-5-codex', name: 'gpt', connection: 'codex', size: '', backend: 'harness', speed: '',
			status: 'unavailable', status_reason: 'usage_limited',
			usage: { state: 'limited', usable: false, reason: 'plan_limit_overage_off', resets_at: null, windows: [] },
		};
		models.set([limited as never]);
		activeModelId.set('codex/gpt-5-codex');
		messages.set([queuedInput()]);
		renderInputBar();
		expect(chipTexts()).toEqual(['Waiting for model: first in line']);

		// A ready model leaves the row as the message alone.
		models.set([{ ...limited, status: 'ready', status_reason: undefined, usage: undefined } as never]);
		flushSync();
		expect(chipTexts()).toEqual(['first in line']);
	});

	it('names an attachment-only queue instead of showing an empty row', () => {
		messages.set([queuedInput({
			content: '',
			metadata: {
				input_id: 'q1', delivery_intent: 'queue_after_turn',
				attachments: [{ file_id: 'f1', filename: 'shot.png', is_image: true }],
			},
		})]);

		renderInputBar();

		expect(chipTexts()).toEqual(['Attachment']);
		expect(host.querySelector('.queued-attach')?.textContent?.trim()).toBe('1');
	});
});
