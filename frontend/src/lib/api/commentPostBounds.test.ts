// Every await between a press of Comment and its comment on screen has a bound, so a post ends delivered or failed,
// never pending. The capture's own bound (RESERVE_BUDGET_MS) is proved in DocViewer.liveComment.test.ts.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { COMMENT_LOOKUP_TIMEOUT_MS, COMMENT_POST_TIMEOUT_MS, PostTimeoutError } from './boundedRequest';
import { fetchComments, postComment, registerArtifact } from './docs';
import { activeDocEditor, ensureEditorArtifactId } from '$lib/stores/activeDocEditor';
import { postTop } from '$lib/stores/docComments';
import { COMMENT_TIMING_HEADER, resetCommentPostTiming, timedPost } from '$lib/stores/commentPostTiming';

const never = () => new Promise<Response>(() => {});
/** An answer whose headers arrive and whose body never ends. */
const stalledBody = () => Promise.resolve(new Response(new ReadableStream({ start() {} }), { status: 200 }));
const fields = { body: 'Too vague', anchor_text: 'beta', from_off: 6, to_off: 10, author: 'you', author_kind: 'user' as const };

/** Whether `promise` has settled, and how. */
function watch<T>(promise: Promise<T>) {
	const state: { settled: boolean; error?: unknown } = { settled: false };
	promise.then(() => { state.settled = true; }, (error) => { state.settled = true; state.error = error; });
	return state;
}

beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); resetCommentPostTiming(); activeDocEditor.set(null); });

describe('each await on a comment post ends inside its bound', () => {
	it.each([
		['the POST answer never comes', never],
		['the POST answer starts and its body never ends', stalledBody],
	])('%s: the post fails with PostTimeoutError at COMMENT_POST_TIMEOUT_MS', async (_case, answer) => {
		vi.stubGlobal('fetch', vi.fn(answer));
		const post = watch(postComment('artifact', { ...fields, client_id: 'draft-1' }));
		await vi.advanceTimersByTimeAsync(COMMENT_POST_TIMEOUT_MS - 1);
		expect(post.settled).toBe(false);
		await vi.advanceTimersByTimeAsync(1);
		expect(post.error).toBeInstanceOf(PostTimeoutError);
	});

	it.each([
		['never comes', never],
		['never finishes its body', stalledBody],
	])('the look for a stored comment whose answer %s fails at its bound', async (_case, answer) => {
		vi.stubGlobal('fetch', vi.fn(answer));
		const look = watch(fetchComments('artifact', COMMENT_LOOKUP_TIMEOUT_MS));
		await vi.advanceTimersByTimeAsync(COMMENT_LOOKUP_TIMEOUT_MS);
		expect(look.error).toBeInstanceOf(PostTimeoutError);
	});

	it('registering the document a comment waits on fails at COMMENT_POST_TIMEOUT_MS', async () => {
		vi.stubGlobal('fetch', vi.fn(never));
		activeDocEditor.set({ instanceId: 'tab', path: '/documents/doc.md', projectId: 'project', threadId: 'thread', artifactId: null } as never);
		const ensured = watch(ensureEditorArtifactId());
		await vi.advanceTimersByTimeAsync(COMMENT_POST_TIMEOUT_MS);
		expect(ensured.error).toBeInstanceOf(PostTimeoutError);
		const direct = watch(registerArtifact('/documents/doc.md', { within: COMMENT_POST_TIMEOUT_MS }));
		await vi.advanceTimersByTimeAsync(COMMENT_POST_TIMEOUT_MS);
		expect(direct.error).toBeInstanceOf(PostTimeoutError);
	});

	it('a POST with no answer, then a look that gets none either, ends failed inside both bounds, never pending', async () => {
		vi.stubGlobal('fetch', vi.fn(never));
		const post = watch(postTop('artifact', 'draft-1', fields));
		await vi.advanceTimersByTimeAsync(COMMENT_POST_TIMEOUT_MS + COMMENT_LOOKUP_TIMEOUT_MS - 1);
		expect(post.settled).toBe(false);
		await vi.advanceTimersByTimeAsync(1);
		expect(post.error).toBeInstanceOf(PostTimeoutError);
	});
});

describe('the timing header', () => {
	const headerOf = (call: unknown[]) => new Headers((call[1] as RequestInit).headers).get(COMMENT_TIMING_HEADER);

	it('carries the before-POST parts on the POST and the after-POST parts on the next comment request, numbers only', async () => {
		vi.useRealTimers();
		const fetch = vi.fn(async (input: string) => input.includes('/comments') && !input.includes('positions') && fetch.mock.calls.length === 1
			? new Response(JSON.stringify({ comment: { id: 'draft-1', body: 'Too vague' } }), { status: 200, headers: { 'Server-Timing': 'app;dur=37.4, dbq;dur=1.0' } })
			: new Response(JSON.stringify({ comments: [], feedback: {} }), { status: 200 }));
		vi.stubGlobal('fetch', fetch);
		await timedPost('draft-1', () => postTop('artifact', 'draft-1', fields));
		await fetchComments('artifact');
		const onPost = headerOf(fetch.mock.calls[0]);
		const onNext = headerOf(fetch.mock.calls[1]);
		expect(onPost).toBe('n=1,a=1');
		expect(onNext).toMatch(/^pn=1,rec=0,out=delivered,total=\d+,post=\d+,app=37$/);
		for (const header of [onPost, onNext]) {
			expect(header).not.toContain('draft-1');
			expect(header).not.toContain('Too vague');
		}
		await fetchComments('artifact');
		expect(headerOf(fetch.mock.calls[2])).toBeNull();
	});

	it('names a press that timed out, its count on the draft, and the look that followed it', async () => {
		vi.stubGlobal('fetch', vi.fn(never));
		const first = watch(timedPost('draft-1', () => postTop('artifact', 'draft-1', fields)));
		await vi.advanceTimersByTimeAsync(COMMENT_POST_TIMEOUT_MS + COMMENT_LOOKUP_TIMEOUT_MS);
		expect(first.error).toBeInstanceOf(PostTimeoutError);
		vi.useRealTimers();
		const fetch = vi.fn(async () => new Response(JSON.stringify({ comment: { id: 'draft-1', body: 'Too vague' } }), { status: 200 }));
		vi.stubGlobal('fetch', fetch);
		await timedPost('draft-1', () => postTop('artifact', 'draft-1', fields));
		expect(headerOf(fetch.mock.calls[0])).toMatch(/^n=2,a=2,pn=1,rec=1,out=timeout,total=\d+,post=\d+,recms=\d+$/);
	});

	it('keeps a failed post\'s report on every comment request until one carrying it is answered successfully', async () => {
		vi.useRealTimers();
		await timedPost('draft-1', () => Promise.reject(new Error('refused'))).catch(() => {});
		const answers = [
			() => Promise.resolve(new Response('{}', { status: 500 })),
			() => Promise.reject(new TypeError('Failed to fetch')),
			() => Promise.resolve(new Response(JSON.stringify({ comments: [], feedback: {} }), { status: 200 })),
			() => Promise.resolve(new Response(JSON.stringify({ comments: [], feedback: {} }), { status: 200 }))
		];
		const fetch = vi.fn(() => answers[fetch.mock.calls.length - 1]());
		vi.stubGlobal('fetch', fetch);
		for (let i = 0; i < 4; i++) await fetchComments('artifact').catch(() => {});
		const headers = fetch.mock.calls.map(headerOf);
		for (const header of headers.slice(0, 3)) expect(header).toMatch(/^pn=1,rec=0,out=failed,total=\d+$/);
		expect(headers[3]).toBeNull();
	});

	it.each([
		['a refusal with its code', new Error('400 {"detail":"the image a.png couldn’t be found.","kind":"region_target"}'), 'failed:400:region_target'],
		['a selection refusal', new Error('409 {"detail":"The text you selected changed.","kind":"selection_changed"}'), 'failed:409:selection_changed'],
		['a refusal with no code', new Error('404 {"detail":"Not found"}'), 'failed:404'],
		['a body that is not JSON', new Error('502 <html>Bad gateway</html>'), 'failed:502'],
		['a code that is a sentence', new Error('400 {"kind":"the image /Users/me/a.png couldn’t be found"}'), 'failed:400'],
		['a lost connection', new TypeError('Failed to fetch'), 'failed']
	])('names how a post failed by its status and its code only: %s', async (_name, error, expected) => {
		vi.useRealTimers();
		await timedPost('draft-1', () => Promise.reject(error)).catch(() => {});
		const fetch = vi.fn(() => Promise.resolve(new Response(JSON.stringify({ comments: [], feedback: {} }), { status: 200 })));
		vi.stubGlobal('fetch', fetch);
		await fetchComments('artifact');
		expect(headerOf(fetch.mock.calls[0])).toMatch(new RegExp(`^pn=1,rec=0,out=${expected},total=\\d+$`));
	});

	it('lets the last finished post win: an earlier request\'s late success does not clear a newer report', async () => {
		vi.useRealTimers();
		await timedPost('draft-1', () => Promise.reject(new Error('refused'))).catch(() => {});
		let answer!: (response: Response) => void;
		const list = () => new Response(JSON.stringify({ comments: [], feedback: {} }), { status: 200 });
		const fetch = vi.fn(() => fetch.mock.calls.length === 1 ? new Promise<Response>((resolve) => { answer = resolve; }) : Promise.resolve(list()));
		vi.stubGlobal('fetch', fetch);
		const first = fetchComments('artifact');
		await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
		await timedPost('draft-2', () => Promise.reject(new Error('refused'))).catch(() => {});
		answer(list());
		await first;
		await fetchComments('artifact');
		await fetchComments('artifact');
		const headers = fetch.mock.calls.map(headerOf);
		expect(headers[0]).toMatch(/^pn=1,/);
		expect(headers[1]).toMatch(/^pn=2,rec=0,out=failed,total=\d+$/);
		expect(headers[2]).toBeNull();
	});
});
