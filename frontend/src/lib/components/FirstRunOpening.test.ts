import { flushSync, mount, tick, unmount } from 'svelte';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import MessageList from './MessageList.svelte';
import { activeThreadId, activeToolsByThread, liveReasoningByThread, messages, streamContentByThread, type Message } from '$lib/stores/threads';
import { coreSettings } from '$lib/stores/settings';
import { token } from '$lib/stores/auth';
import { checkpointsByThread } from '$lib/stores/checkpoints';
import { threadUpdateCards, visitDividers, openAtDivider } from '$lib/stores/threadUpdateCards';
import { _resetTurnStateForTests, INITIAL_TURN_STATE, restoreTurnState } from '$lib/ws/turnState';
import { _resetLiveStepForTests } from './plans/planLiveStep';
import { handleMessage } from '$lib/ws/handlers';
import { awaitFirstRunOpening, _resetFirstRunOpeningRevealForTests } from '$lib/ws/firstRunOpeningReveal';
import { _resetTypewritersForTests } from '$lib/ws/typewriter';

vi.mock('$lib/product_events', () => ({ emitProductEvent: vi.fn(), identifyUser: vi.fn() }));
vi.mock('$lib/ws', () => ({ on: () => () => {}, connect: vi.fn(), disconnect: vi.fn(), isConnected: () => false, reconnectNow: vi.fn(), sendMessage: vi.fn(), stopTurn: vi.fn(), hasActiveTurn: () => false, subscribeThread: vi.fn(), unsubscribeThread: vi.fn(), watchThread: vi.fn(), cancelThreadTypewriters: vi.fn() }));
const thread = 'first-run-ui';
const content = 'Hi Pat.\n\n' + readFileSync(resolve(__dirname,
	'../../../../backend/prompts/system/v1/skills/conductor-bootstrap/references/first-run-opening.md',
), 'utf8').trim();
const opening: Omit<Message, 'thread_id'> = { id: `first-run-opening:${thread}`, role: 'assistant', content, seq: 1,
	created_at: '2026-10-22T00:00:00Z', metadata: { for_user: true, first_run_opening: true } };
let host: HTMLElement;
let component: ReturnType<typeof mount> | undefined;
async function settle() { flushSync(); await tick(); flushSync(); }
async function show() {
	component = mount(MessageList, { target: host, props: { messagesEl: undefined, userIsNearBottom: true,
		ignoreScrollEvents: false, onOpenLightbox: () => {}, historyComplete: true } });
	await settle();
}
function receive(message = opening) { handleMessage({ type: 'message_added', thread_id: thread, message }, () => {}); }
beforeEach(() => {
	vi.useFakeTimers(); vi.stubGlobal('matchMedia', () => ({ matches: false }));
	host = document.createElement('div'); document.body.appendChild(host);
	_resetTurnStateForTests(); _resetLiveStepForTests(); _resetFirstRunOpeningRevealForTests(); _resetTypewritersForTests();
	messages.set([]); activeThreadId.set(thread); token.set(null); coreSettings.set(null);
	activeToolsByThread.set(new Map()); streamContentByThread.set(new Map()); liveReasoningByThread.set(new Map());
	threadUpdateCards.set({}); visitDividers.set({}); openAtDivider.set(null); checkpointsByThread.set(new Map());
});
afterEach(async () => {
	if (component) await unmount(component); component = undefined; host.remove();
	_resetTypewritersForTests(); _resetFirstRunOpeningRevealForTests(); _resetTurnStateForTests(); _resetLiveStepForTests();
	messages.set([]); activeThreadId.set(null); coreSettings.set(null);
	activeToolsByThread.set(new Map()); streamContentByThread.set(new Map()); liveReasoningByThread.set(new Map());
	vi.useRealTimers(); vi.unstubAllGlobals();
});

describe('first-run transcript composition', () => {
	for (const focus of [false, true]) it(`holds first-turn output behind the live opening (Focus ${focus}) and announces once`, async () => {
		coreSettings.set({ features: { focus_mode: focus } } as never);
		await show(); awaitFirstRunOpening(thread); receive(); await settle();
		const region = host.querySelector('[aria-live="polite"][aria-atomic="true"]')!;
		expect(region.textContent).toBe(content);
		const changes: MutationRecord[] = [];
		const observer = new MutationObserver(records => changes.push(...records));
		observer.observe(region, { childList: true, characterData: true, subtree: true });
		vi.advanceTimersByTime(90); await settle();
		expect(host.querySelector('.typewriter-text')?.textContent).toMatch(/^Hi Pat\./);
		expect(host.querySelector('.typewriter-text')?.textContent!.length).toBeLessThan(content.length);
		expect(host.querySelector('.typewriter-text')?.closest('[aria-hidden="true"]')).not.toBeNull();
		restoreTurnState(thread, { ...INITIAL_TURN_STATE, phase: 'active', turnId: 'first', turnOrder: 1 });
		streamContentByThread.set(new Map([[thread, 'FIRST_LIVE_DELTA']]));
		liveReasoningByThread.set(new Map([[thread, 'FIRST_REASONING']]));
		receive({ ...opening, id: 'first-step', content: 'FIRST_MODEL_STEP', seq: 2,
			metadata: { for_user: true, turn_id: 'first' } });
		await settle();
		expect(get(messages).find(row => row.id === 'first-step')?.content).toBe('FIRST_MODEL_STEP');
		expect(host.textContent).not.toContain('FIRST_MODEL_STEP');
		expect(host.textContent).not.toContain('FIRST_LIVE_DELTA');
		expect(host.textContent).not.toContain('FIRST_REASONING');
		vi.advanceTimersByTime(1_910); await settle();
		expect(host.querySelector('.typewriter-text')).toBeNull();
		expect(host.textContent).toContain('FIRST_MODEL_STEP');
		expect(host.querySelector('[aria-live="polite"][aria-atomic="true"]')).toBe(region);
		expect(region.textContent).toBe(content); expect(changes).toHaveLength(0);
		observer.disconnect();
	});
	it.each(['history', 'other device', 'reduced motion'])('%s renders the full opening without animation or a live announcement', async kind => {
		if (kind === 'history') messages.set([{ ...opening, thread_id: thread }] as never);
		await show();
		if (kind === 'reduced motion') { awaitFirstRunOpening(thread); vi.stubGlobal('matchMedia', () => ({ matches: true })); }
		if (kind !== 'history') receive();
		await settle();
		expect(host.textContent).toContain("Here's mine for finishing your setup.");
		expect(host.querySelector('.typewriter-text')).toBeNull();
		expect(host.querySelector('[aria-atomic="true"]')).toBeNull();
	});
});
