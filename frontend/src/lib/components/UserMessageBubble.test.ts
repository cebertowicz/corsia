import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import UserMessageBubble from './UserMessageBubble.svelte';
import { token } from '$lib/stores/auth';
import { activeThreadId, messages } from '$lib/stores/threads';
import { activeModelId, models, type Model } from '$lib/stores/models';
import { WAITING_FOR_LOCAL_MODEL, WAITING_FOR_MODEL } from '$lib/messageDeliveryCopy';
import { _resetTurnStateForTests } from '$lib/ws/turnState';
import { formatTime } from '$lib/time';
import { resetUpdateStoreForTests, updateStatus } from '$lib/update/store';

function humanMsg(overrides: Record<string, unknown> = {}) {
	return {
		id: 'm-human-1',
		thread_id: 'thread-1',
		role: 'user',
		content: 'hello from a human',
		created_at: '2026-01-01T00:00:00',
		metadata: null,
		delivery_state: 'delivered',
		delivery_revision: 1,
		...overrides,
	} as import('$lib/stores/threads').Message;
}

function agentMsg(overrides: Record<string, unknown> = {}) {
	return {
		id: 'm-agent-1',
		role: 'user',
		content: 'hello from an agent',
		created_at: '2026-01-01T00:00:00',
		metadata: {
			sender_kind: 'agent',
			from_thread: 'conductor-1',
			from_title: 'Conductor',
		},
		...overrides,
	};
}

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	token.set('ui-token');
	activeThreadId.set(null);
	messages.set([]);
	_resetTurnStateForTests();
	resetUpdateStoreForTests();
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	token.set(null);
	activeThreadId.set(null);
	messages.set([]);
	_resetTurnStateForTests();
	resetUpdateStoreForTests();
	vi.unstubAllGlobals();
});

function render(msg: unknown): HTMLElement {
	component = mount(UserMessageBubble, {
		target: host,
		props: {
			msg,
			onOpenLightbox: () => {},
		},
	});
	flushSync();
	const wrapper = host.querySelector<HTMLElement>('.user-bubble-wrapper');
	expect(wrapper).not.toBeNull();
	return wrapper!;
}

describe('UserMessageBubble — human message path', () => {
	it('keeps checkpoint-held input visible with Cancel and without Retry or Sending', () => {
		const wrapper = render(humanMsg({
			thread_id: 'held-thread', delivery_state: 'queued', active_turn_id: null,
			write_outcome: 'proved_no_write', delivery_error: 'checkpoint unavailable',
		}));
		expect(wrapper.textContent).toContain('Waiting for checkpoint');
		expect(wrapper.textContent).toContain('hello from a human');
		expect(wrapper.querySelector<HTMLButtonElement>('[data-delivery-action="cancel"]')?.disabled).toBe(false);
		expect(wrapper.querySelector('[data-delivery-action="retry"]')).toBeNull();
		expect(wrapper.querySelector('.delivery-status.sending')).toBeNull();
		expect(wrapper.querySelector('.delivery-failed-mark')).toBeNull();
	});
	it.each([
		['a requested queue', { metadata: { delivery_intent: 'queue_after_turn' } }, 'Sends after the update'],
		['a checkpoint hold', { write_outcome: 'proved_no_write', delivery_error: 'checkpoint unavailable' }, 'Waiting for checkpoint'],
		['an apply-now hold', {}, 'Sends after the update'],
	])('changes only %s to the update delivery promise', (_name, message, expected) => {
		activeThreadId.set('thread-update');
		updateStatus.set({
			branch: 'dev', current_sha: 'a', remote_sha: 'b', commits_behind: 1,
			available: true, checked_at: 1, error: null,
			operation: { operation_id: 'update-1', kind: 'update', status: 'in_flight', phase: 'draining' },
		});
		const wrapper = render(humanMsg({
			thread_id: 'thread-update', delivery_state: 'queued', ...message,
		}));
		expect(wrapper.textContent).toContain(expected);
	});

	it('renders the message content', () => {
		const wrapper = render(humanMsg());
		expect(wrapper.querySelector('.bubble-content')?.textContent).toBe('hello from a human');
	});

	it('keeps human-authored content as text', () => {
		const wrapper = render(humanMsg({ content: '**literal Markdown**' }));
		const content = wrapper.querySelector('.bubble-content');
		expect(content?.textContent).toBe('**literal Markdown**');
		expect(content?.querySelector('strong')).toBeNull();
		expect(content?.classList.contains('md-content')).toBe(false);
	});

	it('has .bubble-col wrapper for DOM structure consistency', () => {
		const wrapper = render(humanMsg());
		expect(wrapper.querySelector('.bubble-col')).not.toBeNull();
	});

	it('does not offer delivered-history editing', () => {
		const wrapper = render(humanMsg());
		expect(wrapper.querySelector('.edit-msg-btn')).toBeNull();
	});

	it('does not have agent-sent class for human messages', () => {
		const wrapper = render(humanMsg());
		expect(wrapper.classList.contains('agent-sent')).toBe(false);
	});

	it('does not render attribution chip for human messages', () => {
		const wrapper = render(humanMsg());
		expect(wrapper.querySelector('.agent-attr')).toBeNull();
	});
});

describe('UserMessageBubble — agent message path', () => {
	it('has agent-sent class', () => {
		const wrapper = render(agentMsg());
		expect(wrapper.classList.contains('agent-sent')).toBe(true);
	});

	it('attribution chip is present', () => {
		const wrapper = render(agentMsg());
		expect(wrapper.querySelector('button.agent-attr')).not.toBeNull();
	});

	it('renders agent-sent content through the canonical Markdown seam', () => {
		const body = [
			'**bold** [link](https://example.com)',
			'',
			'```ts',
			'const answer = 42;',
			'```',
			'<script>window.__agentSendXss = true</script>',
		].join('\n');
		const wrapper = render(agentMsg({ content: body }));
		const content = wrapper.querySelector('.bubble-content');
		expect(content?.classList.contains('md-content')).toBe(true);
		expect(content?.querySelector('strong')?.textContent).toBe('bold');
		expect(content?.querySelector('a')?.getAttribute('href')).toBe('https://example.com');
		expect(content?.querySelector('a')?.getAttribute('target')).toBe('_blank');
		expect(content?.querySelector('.code-block')).not.toBeNull();
		expect(content?.querySelector('script')).toBeNull();
	});
});

describe('UserMessageBubble — delivery state', () => {
	it('does not show a status descriptor while a send is optimistic', () => {
		const wrapper = render(humanMsg({ _optimistic: true }));
		expect(wrapper.querySelector('.delivery-status')).toBeNull();
	});

	it('renders the exact pending-steer descriptor for a canonical apply-now receipt', () => {
		const wrapper = render(humanMsg({
			delivery_state: 'queued', delivery_error: null,
		}));
		const status = wrapper.querySelector<HTMLElement>('.delivery-status');
		expect(status?.textContent).toBe('Sending when ready…');
		expect(status?.title).toBe('CORSIA will send this as soon as the thread can continue.');
		expect(wrapper.classList.contains('provider-pending')).toBe(true);
	});

	it('reads "Waiting for model" while the bound model row is usage-limited', () => {
		activeThreadId.set('thread-held');
		models.set([{
			id: 'codex/gpt-5-codex', name: 'gpt', connection: 'codex', size: '', backend: 'harness', speed: '',
			status: 'unavailable', status_reason: 'usage_limited',
		} as Model]);
		activeModelId.set('codex/gpt-5-codex');
		try {
			const wrapper = render(humanMsg({ delivery_state: 'queued', delivery_error: null, thread_id: 'thread-held' }));
			const status = wrapper.querySelector<HTMLElement>('.delivery-status');
			expect(status?.textContent).toBe('Waiting for model');
			expect(status?.title).toBe(WAITING_FOR_MODEL.title);
		} finally {
			models.set([]);
			activeModelId.set(null);
		}
	});

	it('renders the local-model hold for a cloud message blocked by project policy', () => {
		const wrapper = render(humanMsg({
			delivery_state: 'queued', delivery_error: 'project_cloud_off',
			write_outcome: 'proved_no_write', active_turn_id: null,
		}));
		const status = wrapper.querySelector<HTMLElement>('.delivery-status');
		expect(status?.textContent).toBe(WAITING_FOR_LOCAL_MODEL.label);
		expect(status?.title).toBe(WAITING_FOR_LOCAL_MODEL.title);
	});

	it('hides pending copy and cancel once the server says a turn owns the input', () => {
		const wrapper = render(humanMsg({
			thread_id: 'thread-active',
			delivery_state: 'queued',
			delivery_error: null,
			active_turn_id: 'turn-1',
		}));

		expect(wrapper.querySelector('.delivery-status')).toBeNull();
		expect(wrapper.querySelector('[data-delivery-action="cancel"]')).toBeNull();
		expect(wrapper.classList.contains('provider-pending')).toBe(false);
	});

	it('renders Queued only for an explicit Queue delivery intent', () => {
		const wrapper = render(humanMsg({
			delivery_state: 'queued', delivery_error: null,
			metadata: { delivery_intent: 'queue_after_turn' },
		}));
		const status = wrapper.querySelector<HTMLElement>('.delivery-status');
		expect(status?.textContent).toBe('Queued');
		expect(status?.title).toBe('Queued by request until the current turn completes');
	});

	it('removes delivery treatment after acceptance without showing Delivered', () => {
		const wrapper = render(humanMsg({
			delivery_state: 'delivered', delivery_error: null,
		}));
		expect(wrapper.querySelector('.delivery-status')).toBeNull();
		expect(wrapper.classList.contains('provider-pending')).toBe(false);
	});

	it('keeps a failed message visible with its delivery reason', () => {
		const wrapper = render(humanMsg({
			thread_id: 'thread-failed',
			content: 'keep this thought',
			delivery_state: 'failed',
			delivery_error: 'The selected model is no longer available',
		}));
		const status = wrapper.querySelector<HTMLElement>('.delivery-status');
		expect(status?.textContent).toBe('Not delivered: The selected model is no longer available');
		expect(status?.title).toBe('The selected model is no longer available');
		expect(wrapper.textContent).toContain('keep this thought');
		// The failure is marked where the user is looking and remains retryable.
		expect(wrapper.querySelector('.delivery-failed-mark')).not.toBeNull();
		expect(wrapper.querySelector<HTMLButtonElement>('[data-delivery-action="retry"]'))
			.not.toBeNull();
	});

	it('lets a user cancel a queued durable message without removing its bubble', async () => {
		const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
		vi.stubGlobal('fetch', fetchMock);
		const wrapper = render(humanMsg({
			thread_id: 'thread-queued',
			delivery_state: 'queued',
			delivery_error: null,
		}));

		const cancel = wrapper.querySelector<HTMLButtonElement>('[data-delivery-action="cancel"]');
		expect(cancel?.textContent).toBe('Cancel');
		cancel?.click();

		await vi.waitFor(() => {
			expect(fetchMock).toHaveBeenCalledWith(
				'/api/threads/thread-queued/messages/m-human-1/cancel?token=ui-token',
				{ method: 'POST' },
			);
		});
		expect(wrapper.textContent).toContain('hello from a human');
	});

	it('adopts the canonical cancel response without waiting for a WebSocket frame', async () => {
		const queued = humanMsg({
			thread_id: 'thread-queued',
			delivery_state: 'queued',
			delivery_error: null,
			delivery_revision: 1,
		});
		activeThreadId.set('thread-queued');
		messages.set([queued]);
		const fetchMock = vi.fn().mockResolvedValue({
			ok: true,
			json: async () => ({
				thread_id: 'thread-queued',
				message: {
					...queued,
					delivery_state: 'cancelled',
					delivery_error: 'cancelled_by_user',
					delivery_revision: 2,
				},
			}),
		});
		vi.stubGlobal('fetch', fetchMock);
		const wrapper = render(queued);

		wrapper.querySelector<HTMLButtonElement>('[data-delivery-action="cancel"]')?.click();

		await vi.waitFor(() => {
			expect(get(messages)).toEqual([expect.objectContaining({
				id: 'm-human-1',
				delivery_state: 'cancelled',
				delivery_error: 'cancelled_by_user',
				delivery_revision: 2,
			})]);
		});
	});

	it('warns that a resend may duplicate when the write outcome is unknown', async () => {
		const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
		vi.stubGlobal('fetch', fetchMock);
		const wrapper = render(humanMsg({
			thread_id: 'thread-ambiguous',
			delivery_state: 'ambiguous',
			delivery_error: 'provider_write_outcome_unknown',
		}));

		const retry = wrapper.querySelector<HTMLButtonElement>('[data-delivery-action="retry-anyway"]');
		expect(retry?.textContent).toBe('Retry anyway');
		expect(retry?.title).toContain('twice');
		// `/abandon` is not an endpoint. A control that can only 404 is a dead
		// control on a user surface.
		expect(wrapper.querySelector('[data-delivery-action="abandon"]')).toBeNull();
		retry?.click();

		await vi.waitFor(() => {
			// The endpoint takes no body; the acknowledgement field it was sent
			// was never read by anything.
			expect(fetchMock).toHaveBeenCalledWith(
				'/api/threads/thread-ambiguous/messages/m-human-1/retry?token=ui-token',
				{ method: 'POST' },
			);
		});
	});

	it('renders a cancelled message as terminal, offering no retry', () => {
		const wrapper = render(humanMsg({
			content: 'never mind',
			delivery_state: 'cancelled',
			delivery_error: 'cancelled_by_user',
		}));
		const status = wrapper.querySelector<HTMLElement>('.delivery-status');
		expect(status?.textContent).toBe('Cancelled by you');
		expect(wrapper.querySelector('[data-delivery-action="retry"]')).toBeNull();
		expect(wrapper.querySelector('[data-delivery-action="retry-anyway"]')).toBeNull();
		expect(wrapper.querySelector('[data-delivery-action="abandon"]')).toBeNull();
	});

	it('hides the timestamp while a message is still pending', () => {
		const created_at = '2026-01-01T12:34:00';
		const wrapper = render(humanMsg({ created_at, delivery_state: 'queued', delivery_error: null }));
		expect(wrapper.querySelector('.msg-time')?.textContent).not.toContain(formatTime(created_at));
	});

	it('shows the timestamp once the message has delivered', () => {
		const created_at = '2026-01-01T12:34:00';
		const wrapper = render(humanMsg({ created_at, delivery_state: 'delivered' }));
		expect(wrapper.querySelector('.msg-time')?.textContent).toContain(formatTime(created_at));
	});

	// An ending is an ending. A cancelled bubble sits in the transcript between
	// two messages that both show a time; showing none reads as "this one has
	// not happened yet", which is the opposite of what cancel means.
	it.each([
		['cancelled', 'cancelled_by_user', 'a cancellation'],
		['failed', 'provider_unavailable', 'a failed delivery'],
	] as const)('shows the timestamp on an ended message (%s)', (delivery_state, delivery_error, _description) => {
		const created_at = '2026-01-01T12:34:00';
		const wrapper = render(humanMsg({
			created_at,
			delivery_state,
			delivery_error,
		}));
		expect(wrapper.querySelector('.msg-time')?.textContent).toContain(formatTime(created_at));
	});

	it('still hides the timestamp on an optimistic echo the server has not seen', () => {
		const created_at = '2026-01-01T12:34:00';
		const wrapper = render(humanMsg({
			created_at,
			delivery_state: undefined,
			_optimistic: true,
		}));
		expect(wrapper.querySelector('.msg-time')?.textContent).not.toContain(formatTime(created_at));
	});

	it('retries an ordinary failed delivery without an ambiguity acknowledgement', async () => {
		const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
		vi.stubGlobal('fetch', fetchMock);
		const wrapper = render(humanMsg({
			thread_id: 'thread-retry',
			delivery_state: 'failed',
			delivery_error: 'provider_unavailable',
		}));

		const retry = wrapper.querySelector<HTMLButtonElement>('[data-delivery-action="retry"]');
		expect(retry?.textContent).toBe('Retry');
		retry?.click();

		await vi.waitFor(() => {
			expect(fetchMock).toHaveBeenCalledWith(
				'/api/threads/thread-retry/messages/m-human-1/retry?token=ui-token',
				{ method: 'POST' },
			);
		});
	});
	it('gives a parked agent send the same exit a human message has', () => {
		// An agent send parks on our own defect exactly like any other queued
		// message, and a parked head's only exit is this control. Hiding it left
		// agent rows with no exit at all: the backend escape names the user's
		// explicit retry, and there was nothing to press.
		activeThreadId.set('thread-agent-parked');
		const wrapper = render(agentMsg({
			thread_id: 'thread-agent-parked',
			delivery_state: 'queued',
			delivery_error: null,
			delivering_reason: 'delivery_defect',
		}));

		expect(wrapper.querySelector('[data-delivery-action="retry"]')).not.toBeNull();
		expect(wrapper.querySelector('[data-delivery-action="cancel"]')).not.toBeNull();
	});
});
