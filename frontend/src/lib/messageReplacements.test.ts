import { beforeEach, afterEach, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { activeThreadId, messages, type Message } from './stores/threads';
import { token } from './stores/auth';
import { adoptThreadSnapshot, claimDisplayedThread, resetDisplayedTranscript, _resetDisplayedThreadsForTests } from './stores/displayedThreads';
import { resetReplacementsForTests } from './messageReplacements';
import { handleMessage } from './ws/handlers';
import { loadRunRange } from './focusRunLoader';
import { apiFetch } from './api/request';
import { emit } from './ws/events';
import { subscribeChatPanelEvents } from './components/chatPanelEvents';

vi.mock('./api/request', () => ({ apiFetch: vi.fn() }));
const row = (id: string, seq: number, thread = 'main', replaced: string[] = []) => ({
	id, seq, thread_id: thread, role: 'assistant', content: id,
	created_at: '2026-11-14T01:00:00', metadata: { for_user: true }, replaced_message_ids: replaced,
}) as Message;
const frame = (id: string, seq: number, removed: string[], thread = 'main') => handleMessage({
	type: 'messages_replaced', thread_id: thread, removed_message_ids: removed,
	message: row(id, seq, thread, removed),
}, () => {});

beforeEach(() => {
	resetReplacementsForTests(); _resetDisplayedThreadsForTests();
	activeThreadId.set('main'); messages.set([]); token.set('test'); vi.clearAllMocks();
});
afterEach(() => { token.set(null); resetReplacementsForTests(); messages.set([]); });

it('swaps atomically on the active transcript and on a claimed leaf', () => {
	claimDisplayedThread('leaf');
	messages.set([row('a', 1), row('la', 1, 'leaf')]);
	const observed: string[][] = [];
	const stop = messages.subscribe(rows => observed.push(rows.map(row => row.id)));
	frame('b', 2, ['a']); frame('lb', 2, ['la'], 'leaf'); stop();
	expect(observed).toEqual([['a', 'la'], ['la', 'b'], ['b', 'lb']]);
});

it('rejects delayed adds, snapshots, older pages and duplicate chained frames', () => {
	messages.set([row('a', 1)]);
	frame('c', 3, ['a', 'b']);
	frame('b', 2, ['a']); // Out of order predecessor cannot come back.
	frame('c', 3, ['a', 'b']);
	handleMessage({ type: 'message_added', thread_id: 'main', message: row('a', 1) }, () => {});
	handleMessage({ type: 'messages_added', thread_id: 'main', messages: [row('b', 2)] }, () => {});
	adoptThreadSnapshot('main', [row('a', 1), row('b', 2)]);
	expect(get(messages).map(row => row.id)).toEqual(['c']);
});

it('learns removal ancestry from a fresh page after a missed frame', () => {
	messages.set([row('a', 1)]);
	adoptThreadSnapshot('main', [row('c', 3, 'main', ['a', 'b'])]);
	adoptThreadSnapshot('main', [row('b', 2)]);
	expect(get(messages).map(row => row.id)).toEqual(['c']);
});

it('reconnect drops retained rows before fetching the fresh page', () => {
	token.set(null); // No unrelated checkpoint hydration in this event test.
	messages.set([row('a', 1)]);
	const fetchThreadMessages = vi.fn(async () => {});
	const stop = subscribeChatPanelEvents({ activeThreadId, fetchThreadMessages,
		isNearBottom: () => false, scrollToBottom: vi.fn(), followBottom: vi.fn(), clearWarmKeys: vi.fn() });
	emit('connected', {}); stop();
	expect(get(messages)).toEqual([]);
	expect(fetchThreadMessages).toHaveBeenCalledWith('main', true);
});

it('a range response begun before reconnect cannot resurrect missed replacements', async () => {
	let resolve!: (response: Response) => void;
	vi.mocked(apiFetch).mockReturnValueOnce(new Promise(done => { resolve = done; }));
	const setLoad = vi.fn();
	const read = loadRunRange({ firstId: 'a', lastId: 'a', firstSeq: 1, lastSeq: 1,
		count: 1, turnIds: [], weight: 1, kinds: [], continuesOlder: false },
		{ pageSize: 50, loads: () => ({}), setLoad, onSummaries: vi.fn() });
	resetDisplayedTranscript('main');
	adoptThreadSnapshot('main', [row('b', 2)]);
	resolve(new Response(JSON.stringify({ messages: [row('a', 1)] })));
	await read;
	expect(get(messages).map(row => row.id)).toEqual(['b']);
	expect(setLoad).toHaveBeenCalledTimes(1);
});
