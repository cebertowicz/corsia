import { describe, expect, it } from 'vitest';
import {
	deliveryPresentation as presentDelivery,
	isAwaitingDelivery as awaitingDelivery,
	isImminentDelivery as imminentDelivery,
	isProviderPendingApplyNow as providerPendingApplyNow,
	isQueuedAfterTurn,
	readMessageDeliveryState,
} from './messageDelivery';

// Most unit cases have no live turn; the echo cases below pass their fact directly.
const deliveryPresentation = (message: Parameters<typeof presentDelivery>[0]) =>
	presentDelivery(message, false);
const isAwaitingDelivery = (message: Parameters<typeof awaitingDelivery>[0]) =>
	awaitingDelivery(message, false);
const isImminentDelivery = (message: Parameters<typeof imminentDelivery>[0]) =>
	imminentDelivery(message, false);
const isProviderPendingApplyNow = (message: Parameters<typeof providerPendingApplyNow>[0]) =>
	providerPendingApplyNow(message, false);

describe('deliveryPresentation', () => {
	it.each(['apply_now', 'queue_after_turn'])('shows a checkpoint hold without claiming delivery for %s', (intent) => {
		const held = {
			delivery_state: 'queued' as const, write_outcome: 'proved_no_write' as const,
			active_turn_id: null, delivery_error: 'checkpoint unavailable',
			metadata: { delivery_intent: intent },
		};
		expect(deliveryPresentation(held)).toMatchObject({
			label: 'Waiting for checkpoint', pending: true, failed: false,
		});
		expect(isAwaitingDelivery(held)).toBe(true);
		expect(isProviderPendingApplyNow(held)).toBe(false);
	});
	it('shows a distinct hold for a selected model unavailable, not the checkpoint label', () => {
		const held = {
			delivery_state: 'queued' as const, write_outcome: 'proved_no_write' as const,
			active_turn_id: null, delivery_error: 'selected_model_unavailable',
			metadata: {},
		};
		expect(deliveryPresentation(held)).toMatchObject({
			label: 'Waiting for model',
			title: 'The selected model is unavailable. Your message is saved and will send when the model is back, or as soon as you switch models.',
			pending: true,
			failed: false,
		});
	});
	it('shows the local-model hold for a message blocked by project cloud policy', () => {
		const held = {
			delivery_state: 'queued' as const, write_outcome: 'proved_no_write' as const,
			active_turn_id: null, delivery_error: 'project_cloud_off',
			metadata: {},
		};
		expect(deliveryPresentation(held)).toEqual({
			label: 'Switch to a local model to send',
			title: 'Local models only is on for this project. Your message is saved and will send as soon as you switch this chat to a local model or turn Local models only off.',
			pending: true,
			failed: false,
		});
	});
	it('shows the chat-local-only hold without offering the project policy escape', () => {
		const held = {
			delivery_state: 'queued' as const, write_outcome: 'proved_no_write' as const,
			active_turn_id: null, delivery_error: 'thread_local_only',
			metadata: {},
		};
		expect(deliveryPresentation(held)).toEqual({
			label: 'Switch to a local model to send',
			title: 'Local only is on for this chat. Your message is saved and will send as soon as you switch this chat to a local model.',
			pending: true,
			failed: false,
		});
	});
	it('asks for a model when the thread has none, with the same pending treatment', () => {
		const held = {
			delivery_state: 'queued' as const, write_outcome: 'proved_no_write' as const,
			active_turn_id: null, delivery_error: 'no_model_selected',
			metadata: {},
		};
		expect(deliveryPresentation(held)).toEqual({
			label: 'Choose a model to send',
			title: 'This thread has no model yet. Your message is saved and will send as soon as you choose one.',
			pending: true,
			failed: false,
		});
		expect(isAwaitingDelivery(held)).toBe(true);
		expect(isProviderPendingApplyNow(held)).toBe(false);
	});
	it('keeps the checkpoint label for every other held reason', () => {
		const held = {
			delivery_state: 'queued' as const, write_outcome: 'proved_no_write' as const,
			active_turn_id: null, delivery_error: 'checkpoint unavailable',
			metadata: {},
		};
		expect(deliveryPresentation(held)).toMatchObject({
			label: 'Waiting for checkpoint', pending: true, failed: false,
		});
	});
	it('shows no descriptor while an ordinary send is still optimistic', () => {
		expect(deliveryPresentation({ _optimistic: true })).toBeNull();
	});

	it('shows no descriptor for a mid-turn echo either — the caption waits for the receipt', () => {
		// The echo holds the waiting region below the in-flight reply (#1087)
		// from its own claim plus the live turn, but the caption still waits
		// for the receipt: only the server can say what delivery is doing.
		const echo = { _optimistic: true, thread_id: 'thread-1' };
		expect(presentDelivery(echo, true)).toBeNull();
	});

	it('shows general pending copy for a durable apply-now queue receipt', () => {
		const message = { delivery_state: 'queued' as const };
		expect(deliveryPresentation(message)).toMatchObject({
			label: 'Sending when ready…',
			title: 'CORSIA will send this as soon as the thread can continue.',
			pending: true,
		});
		expect(isProviderPendingApplyNow(message)).toBe(true);
	});

	it('shows no pending treatment once the queued input belongs to the active turn', () => {
		const message = {
			delivery_state: 'queued' as const,
			active_turn_id: 'turn-1',
		};
		expect(deliveryPresentation(message)).toBeNull();
		expect(isProviderPendingApplyNow(message)).toBe(false);
	});

	it('shows Queued for an explicit Queue until a turn claims it', () => {
		expect(deliveryPresentation({
			delivery_state: 'queued',
			metadata: { delivery_intent: 'queue_after_turn' },
		})).toMatchObject({ label: 'Queued', pending: true });
	});

	it('stops offering queued controls when the next turn claims an explicit Queue', () => {
		expect(deliveryPresentation({
			delivery_state: 'queued',
			active_turn_id: 'turn-2',
			metadata: { delivery_intent: 'queue_after_turn' },
		})).toBeNull();
	});

	it('calls a queue queued from the optimistic echo, before any receipt', () => {
		// Every other descriptor reports something only the server knows. This one
		// reports what the user just asked for, which is true from the moment they
		// ask it — so it is not suppressed for the optimistic round trip.
		expect(deliveryPresentation({
			_optimistic: true,
			metadata: { delivery_intent: 'queue_after_turn' },
		})).toMatchObject({ label: 'Queued', pending: true, failed: false });
	});

	it('stops calling a queue queued once it ends', () => {
		for (const ended of [
			{ delivery_state: 'delivered' as const },
			{ delivery_state: 'cancelled' as const, delivery_error: 'cancelled_by_user' },
		]) {
			const message = { ...ended, metadata: { delivery_intent: 'queue_after_turn' } };
			expect(isQueuedAfterTurn(message)).toBe(false);
			expect(isAwaitingDelivery(message)).toBe(false);
			expect(deliveryPresentation(message)?.label).not.toBe('Queued');
		}
	});
});

describe('isAwaitingDelivery', () => {
	// A queued input has no delivered transcript position, so it remains in the
	// waiting region until the server assigns one.
	it('holds every input that has not yet earned a sequence position', () => {
		const waiting = [
			// Apply-now, waiting for a provider-safe boundary.
			{ delivery_state: 'queued' as const },
			// Explicitly queued by the user until the turn completes.
			{
				delivery_state: 'queued' as const,
				metadata: { delivery_intent: 'queue_after_turn' },
			},
			// Optimistic echo of that same queue, before its receipt.
			{ _optimistic: true, metadata: { delivery_intent: 'queue_after_turn' } },
			// Parked on a CORSIA defect, which nothing automatic will move.
			{ delivery_state: 'queued' as const, delivering_reason: 'delivery_defect' },
		];
		for (const message of waiting) expect(isAwaitingDelivery(message)).toBe(true);
	});

	it('releases an input the moment it holds a sequence, delivered or not', () => {
		expect(isAwaitingDelivery({ delivery_state: 'delivered' })).toBe(false);
		expect(isAwaitingDelivery({
			delivery_state: 'cancelled', delivery_error: 'cancelled_by_user',
		})).toBe(false);
		expect(isAwaitingDelivery({
			delivery_state: 'failed', delivery_error: 'provider_unavailable',
		})).toBe(false);
	});

	it('releases an apply-now input the active turn already holds', () => {
		expect(isAwaitingDelivery({
			delivery_state: 'queued', active_turn_id: 'turn-1',
		})).toBe(false);
	});

	it('splits the region into the steer half and the queue half', () => {
		const steer = { delivery_state: 'queued' as const };
		const queue = {
			delivery_state: 'queued' as const,
			metadata: { delivery_intent: 'queue_after_turn' },
		};
		expect(isProviderPendingApplyNow(steer)).toBe(true);
		expect(isQueuedAfterTurn(steer)).toBe(false);
		expect(isProviderPendingApplyNow(queue)).toBe(false);
		expect(isQueuedAfterTurn(queue)).toBe(true);
	});

	it('holds a receipt-less mid-turn echo out of the sequence, below the running reply', () => {
		// #1087: an apply_now send into a running turn has not earned its
		// durable position yet. The whole pre-receipt claim is the echo itself
		// plus the thread's live turn — no server field is written to say so —
		// so it renders in the waiting region until the first receipt replaces
		// the claim with server truth, and it does not move when that arrives.
		const echo = { _optimistic: true, thread_id: 'thread-1' };
		expect(awaitingDelivery(echo, true)).toBe(true);
		expect(imminentDelivery(echo, true)).toBe(true);
	});

	it('keeps an idle-thread echo an ordinary row at the live edge', () => {
		// No turn is running, so the send starts the next one: the row lands
		// at the live edge, and nothing about that wait needs a region.
		const echo = { _optimistic: true, thread_id: 'thread-1' };
		expect(awaitingDelivery(echo, false)).toBe(false);
		expect(imminentDelivery(echo, false)).toBe(false);
	});
});

describe('isImminentDelivery', () => {
	// The in-canvas pending region means one thing: going into the step running
	// now. A parked queue is the opposite instruction, so it is not in it — it
	// waits on the composer instead, and the two are told apart by where they
	// are rather than by what their captions say.
	it('holds every input that is about to be delivered, failures included', () => {
		const imminent = [
			// Apply-now, waiting for a provider-safe boundary.
			{ delivery_state: 'queued' as const },
			{ delivery_state: 'queued' as const, delivering_reason: 'delivery_defect' },
		];
		for (const message of imminent) expect(isImminentDelivery(message)).toBe(true);
	});

	it('excludes an input the user deliberately parked until the turn ends', () => {
		const queue = {
			delivery_state: 'queued' as const,
			metadata: { delivery_intent: 'queue_after_turn' },
		};
		const optimisticQueue = {
			_optimistic: true,
			metadata: { delivery_intent: 'queue_after_turn' },
		};
		for (const message of [queue, optimisticQueue]) {
			// Out of the canvas region, and out of the delivered sequence too.
			expect(isImminentDelivery(message)).toBe(false);
			expect(isAwaitingDelivery(message)).toBe(true);
		}
	});

	it('excludes anything that already holds a sequence position', () => {
		expect(isImminentDelivery({ delivery_state: 'delivered' })).toBe(false);
		expect(isImminentDelivery({
			delivery_state: 'cancelled', delivery_error: 'cancelled_by_user',
		})).toBe(false);
		expect(isImminentDelivery({
			delivery_state: 'queued', active_turn_id: 'turn-1',
		})).toBe(false);
	});

	it('keeps an ambiguous provider write visibly uncertain', () => {
		const message = {
			delivery_state: 'ambiguous' as const,
			delivery_error: 'provider_write_outcome_unknown',
		};
		expect(deliveryPresentation(message)).toMatchObject({ label: 'Delivery uncertain', pending: true });
		expect(isProviderPendingApplyNow(message)).toBe(false);
	});

	it('renders other durable delivery failures in plain language', () => {
		// "Blocked" said the wrong thing about the thread. A failed message is a
		// visible dead message, not a wedged thread.
		expect(deliveryPresentation({
			delivery_state: 'failed', delivery_error: 'provider_unavailable',
		})).toMatchObject({
			label: 'Not delivered: the provider was unreachable',
			pending: true,
			failed: true,
		});
	});

	it('never renders a raw machine code to the user', () => {
		// Being shown `provider_write_outcome_unknown` and asked what to do is
		// asking the user a question they hold no evidence to answer.
		for (const code of [
			'provider_write_outcome_unknown',
			'delivery_descriptor_missing',
			'some_code_nobody_wrote_copy_for',
		]) {
			const presented = deliveryPresentation({
				delivery_state: 'failed', delivery_error: code,
			});
			expect(presented?.label).not.toContain(code);
			expect(presented?.title).not.toContain(code);
		}
	});

	it('says what an unconfirmed write actually means, and what it costs to retry', () => {
		// USER-REPORTS #6c: this rendered as `Blocked: provider_write_outcome_unknown`.
		const presented = deliveryPresentation({
			delivery_state: 'ambiguous',
			delivery_error: 'provider_write_outcome_unknown',
		});
		expect(presented?.label).toBe('Delivery uncertain');
		expect(presented?.title).toContain('may arrive twice');
		expect(presented?.failed).toBe(true);
	});

	it('marks exactly the states that will not move without the user', () => {
		const failed = (message: Parameters<typeof deliveryPresentation>[0]) =>
			deliveryPresentation(message)?.failed;

		expect(failed({ delivery_state: 'failed', delivery_error: 'target_invalid' })).toBe(true);
		expect(failed({ delivery_state: 'ambiguous' })).toBe(true);
		expect(failed({ delivery_state: 'cancelled', delivery_error: 'cancelled_by_user' })).toBe(false);
		expect(failed({ delivery_state: 'queued' })).toBe(false);
	});

	it('never tells the user a defect-parked input is sending', () => {
		// CORSIA stops rather than retrying its own bug, so nothing is sending
		// this and nothing will. Reporting it as ready to send is the
		// exact silence that let inputs show pending forever while never
		// arriving and never failing.
		const parked = deliveryPresentation({
			delivery_state: 'queued',
			delivery_error: null,
			delivering_reason: 'delivery_defect',
			metadata: { turn_id: 'turn-1' },
		});
		expect(parked).toMatchObject({ label: 'Not sent', failed: true, pending: true });
		expect(parked?.title).toContain('retry it, or cancel it');
	});

	it('renders a cancelled message as terminal, not blocked', () => {
		expect(deliveryPresentation({
			delivery_state: 'cancelled', delivery_error: 'cancelled_by_user',
		})).toMatchObject({ label: 'Cancelled by you', title: 'Cancelled by you', pending: true });
	});

	it.each([
		['held_while_paused', 'Held while paused', 'digest'],
		['stale_plan_wake', 'Not needed', 'moved on'],
	])('names why CORSIA set aside a %s input, never blaming the user', (error, label, why) => {
		const shown = deliveryPresentation({ delivery_state: 'cancelled', delivery_error: error });
		expect(shown).toMatchObject({ label, pending: true, failed: false });
		expect(shown?.title).toContain(why);
	});

	it('shows no persistent descriptor after delivery acceptance', () => {
		expect(deliveryPresentation({ delivery_state: 'delivered' })).toBeNull();
		expect(isProviderPendingApplyNow({ delivery_state: 'delivered' })).toBe(false);
	});

	it('accepts only the canonical backend delivery states', () => {
		for (const state of ['queued', 'delivered', 'ambiguous', 'cancelled', 'failed']) {
			expect(readMessageDeliveryState(state)).toBe(state);
		}
		expect(readMessageDeliveryState('settled_elsewhere')).toBeNull();
	});
});
