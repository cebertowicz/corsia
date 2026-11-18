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
});
