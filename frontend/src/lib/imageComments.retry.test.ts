// An image's new comment posts under its draft's one id, as a document's does: a press after a POST the server stored
// but never answered stores no second comment, and text edited since shows the stored version and keeps the edit.
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';

const api = vi.hoisted(() => ({ postComment: vi.fn(), fetchComments: vi.fn() }));
vi.mock('$lib/api/docs', async (original) => ({ ...(await original<object>()), ...api }));
vi.mock('$lib/stores/toast', () => ({ toasts: { show: vi.fn(() => 'toast'), dismiss: vi.fn() } }));

import { COMMENT_POST_TIMEOUT_MS, PostTimeoutError } from '$lib/api/boundedRequest';
import { docComments, EARLIER_VERSION_POSTED } from '$lib/stores/docComments';
import { imageDraftKey, keptCommentDrafts } from '$lib/stores/keptCommentDrafts.svelte';
import { addDocImageComment } from '$lib/stores/imageRegionComments';
import { resetCommentPostTiming } from '$lib/stores/commentPostTiming';
import { createImageComments } from './imageComments.svelte';

const BOX = { x: 0.1, y: 0.2, w: 0.3, h: 0.1 };
const rect = () => new DOMRect(10, 10, 40, 20);
const TEXT = '# Notes\n\n![Mock](a.png)\n';
const imageKey = imageDraftKey('image');
const store = { rows: new Map<string, Record<string, unknown>>(), unanswered: false, lookupFails: false };

beforeEach(() => {
	vi.spyOn(console, 'error').mockImplementation(() => {});
	api.fetchComments.mockImplementation(async (_artifact: string, within?: number) => {
		if (within && store.lookupFails) throw new PostTimeoutError(within);
		return { comments: [...store.rows.values()], feedback: {} };
	});
	api.postComment.mockImplementation(async (_artifact: string, fields: Record<string, any>) => {
		const stored = store.rows.get(fields.client_id);
		if (stored) return stored;
		const row = { id: fields.client_id, body: fields.body.trim(), author: 'you', author_kind: 'user', resolved: false, replies: [], created_at: '2026-11-18T00:00:00Z',
			region_x: BOX.x, region_y: BOX.y, region_w: BOX.w, region_h: BOX.h, region_number: store.rows.size + 1, anchor_text: fields.anchor_text ?? null,
			tagged_agent_ids: [...new Set<string>(fields.tagged_agent_ids ?? [])].sort() };
		store.rows.set(fields.client_id, row);
		if (store.unanswered) { store.unanswered = false; throw new PostTimeoutError(COMMENT_POST_TIMEOUT_MS); }
		return row;
	});
});
afterEach(() => {
	store.rows.clear(); store.unanswered = false; store.lookupFails = false;
	keptCommentDrafts.reset(); docComments.set({}); resetCommentPostTiming(); vi.clearAllMocks(); vi.restoreAllMocks();
});

const surfaces = [
	['an image artifact', 'image', () => createImageComments({ artifactId: () => 'image', readOnly: () => false, restoreFocus: vi.fn() })],
	['an image inside a document', 'doc', () => createImageComments({
		artifactId: () => 'doc', draftKey: () => 'image:doc:a.png', readOnly: () => false, restoreFocus: vi.fn(),
		post: () => ({ create: (region, text, tags, mentions, press) => addDocImageComment({ artifactId: 'doc', from: 9, to: 23, text: TEXT }, region, text, tags, null, mentions, press) }),
	})],
] as const;

for (const [kind, artifact, make] of surfaces) {
	it(`${kind}: leg (i), text edited after an unanswered press shows the first version and posts the edit as its own comment`, async () => {
		const comments = make();
		await comments.draw(BOX, rect);
		store.unanswered = true; store.lookupFails = true;
		await expect(comments.submit('Too dark')).rejects.toBeInstanceOf(PostTimeoutError);
		expect(store.rows.size).toBe(1);
		store.lookupFails = false;
		await comments.draw(BOX, rect);
		expect(comments.draft?.body).toBe('Too dark');
		comments.setDraftBody('Too dark: lift the shadows');
		await expect(comments.submit('Too dark: lift the shadows')).rejects.toThrow(EARLIER_VERSION_POSTED);
		expect(comments.draft?.error).toBe(EARLIER_VERSION_POSTED);
		expect(comments.draft?.body).toBe('Too dark: lift the shadows');
		expect((get(docComments)[artifact] ?? []).map(comment => comment.text)).toEqual(['Too dark']);
		expect(store.rows.size).toBe(1);
		await comments.submit('Too dark: lift the shadows');
		expect([...store.rows.values()].map(row => row.body)).toEqual(['Too dark', 'Too dark: lift the shadows']);
		const ids = api.postComment.mock.calls.map(([, fields]) => fields.client_id);
		expect(ids[0]).not.toBe(ids.at(-1));
	});

	it(`${kind}: the same text pressed again after an unanswered press is delivered once`, async () => {
		const comments = make();
		await comments.draw(BOX, rect);
		store.unanswered = true; store.lookupFails = true;
		await expect(comments.submit('Too dark')).rejects.toBeInstanceOf(PostTimeoutError);
		store.lookupFails = false;
		await comments.submit('Too dark');
		expect(store.rows.size).toBe(1);
		expect((get(docComments)[artifact] ?? []).map(comment => comment.text)).toEqual(['Too dark']);
		expect(new Set(api.postComment.mock.calls.map(([, fields]) => fields.client_id)).size).toBe(1);
	});

	it(`${kind}: a box drawn while a post is out keeps its text and tags when that post lands, and posts under its own id`, async () => {
		const comments = make();
		await comments.draw(BOX, rect);
		comments.setDraftBody('Too dark');
		let land!: () => void;
		const held = new Promise<void>(resolve => { land = resolve; });
		const post = api.postComment.getMockImplementation()!;
		api.postComment.mockImplementationOnce(async (...args: [string, Record<string, any>]) => { await held; return post(...args); });
		const first = comments.submit('Too dark');
		await comments.draw({ ...BOX, x: 0.5 }, rect);
		const tags = comments.draft!.mentionSelection;
		comments.setDraftBody('Too dark, and the sky is flat');
		land();
		await first;
		expect(comments.draft?.body).toBe('Too dark, and the sky is flat');
		expect(comments.draft?.mentionSelection).toBe(tags);
		const kept = keptCommentDrafts.get(artifact === 'image' ? imageKey : 'image:doc:a.png');
		expect(kept?.body).toBe('Too dark, and the sky is flat');
		expect(kept?.clientId).toBeUndefined();
		await comments.submit('Too dark, and the sky is flat');
		expect([...store.rows.values()].map(row => row.body)).toEqual(['Too dark', 'Too dark, and the sky is flat']);
		expect(new Set(api.postComment.mock.calls.map(([, fields]) => fields.client_id)).size).toBe(2);
	});

	it(`${kind}: a box drawn while a post is out, left unedited, is that draft: it closes when the post lands and nothing is stored twice`, async () => {
		const comments = make();
		await comments.draw(BOX, rect);
		comments.setDraftBody('Too dark');
		let land!: () => void;
		const held = new Promise<void>(resolve => { land = resolve; });
		const post = api.postComment.getMockImplementation()!;
		api.postComment.mockImplementationOnce(async (...args: [string, Record<string, any>]) => { await held; return post(...args); });
		const first = comments.submit('Too dark');
		await comments.draw({ ...BOX, x: 0.5 }, rect);
		expect(comments.draft?.body).toBe('Too dark');
		land();
		await first;
		expect(comments.draft).toBeNull();
		expect(comments.hasDraftText).toBe(false);
		expect(keptCommentDrafts.get(artifact === 'image' ? imageKey : 'image:doc:a.png')).toBeUndefined();
		expect(store.rows.size).toBe(1);
	});

	it(`${kind}: a box reopened in a new viewer after its post went out posts unedited under the same id, and one comment is stored`, async () => {
		const closed = make();
		await closed.draw(BOX, rect);
		closed.setDraftBody('Too dark');
		let land!: () => void;
		const held = new Promise<void>(resolve => { land = resolve; });
		const post = api.postComment.getMockImplementation()!;
		api.postComment.mockImplementationOnce(async (...args: [string, Record<string, any>]) => { await held; return post(...args); });
		const first = closed.submit('Too dark');
		const comments = make();
		await comments.draw({ ...BOX, x: 0.5 }, rect);
		expect(comments.draft?.body).toBe('Too dark');
		land();
		await first;
		await comments.submit('Too dark');
		expect(store.rows.size).toBe(1);
		expect(new Set(api.postComment.mock.calls.map(([, fields]) => fields.client_id)).size).toBe(1);
		expect(comments.draft).toBeNull();
	});

	it(`${kind}: a box reopened in a new viewer whose press fails posts again under the same id, and one comment is stored`, async () => {
		const closed = make();
		await closed.draw(BOX, rect);
		closed.setDraftBody('Too dark');
		let land!: () => void;
		const held = new Promise<void>(resolve => { land = resolve; });
		const post = api.postComment.getMockImplementation()!;
		api.postComment.mockImplementationOnce(async (...args: [string, Record<string, any>]) => { await held; return post(...args); });
		const first = closed.submit('Too dark');
		const comments = make();
		await comments.draw({ ...BOX, x: 0.5 }, rect);
		land();
		await first;
		// The POST goes unanswered and the look for what it may have stored fails too, so the press fails.
		api.postComment.mockRejectedValueOnce(new PostTimeoutError(COMMENT_POST_TIMEOUT_MS)); store.lookupFails = true;
		await expect(comments.submit('Too dark')).rejects.toBeInstanceOf(PostTimeoutError);
		expect(comments.draft?.body).toBe('Too dark');
		store.lookupFails = false;
		await comments.submit('Too dark');
		expect(store.rows.size).toBe(1);
		expect(new Set(api.postComment.mock.calls.map(([, fields]) => fields.client_id)).size).toBe(1);
		expect(keptCommentDrafts.get(artifact === 'image' ? imageKey : 'image:doc:a.png')).toBeUndefined();
	});

	it(`${kind}: text and tags edited while the post is out stay kept, whatever the box that pressed clears`, async () => {
		const comments = make();
		await comments.draw(BOX, rect);
		comments.setDraftBody('Ask');
		let land!: () => void;
		const held = new Promise<void>(resolve => { land = resolve; });
		const post = api.postComment.getMockImplementation()!;
		api.postComment.mockImplementationOnce(async (...args: [string, Record<string, any>]) => { await held; return post(...args); });
		const first = comments.submit('Ask');
		const tags = comments.draft!.mentionSelection;
		tags.update('Ask @Ada'); tags.select('agent-ada', 4, '@Ada');
		comments.setDraftBody('Ask @Ada');
		land();
		await first;
		// The box that pressed empties its own tags once its post is stored (CommentComposer).
		tags.clear();
		const kept = keptCommentDrafts.get(artifact === 'image' ? imageKey : 'image:doc:a.png')!;
		expect(kept.body).toBe('Ask @Ada');
		expect(kept.mentionSelection.occurrences(kept.body).map((item) => item.agent_id)).toEqual(['agent-ada']);
	});

	it(`${kind}: a post that fails keeps what the field holds now, not the text it sent`, async () => {
		const comments = make();
		await comments.draw(BOX, rect);
		let fail!: () => void;
		const held = new Promise<void>(resolve => { fail = resolve; });
		api.postComment.mockImplementationOnce(async () => { await held; throw new Error('500 boom'); });
		const first = comments.submit('Too dark');
		comments.setDraftBody('Too dark, and the sky is flat');
		fail();
		await expect(first).rejects.toThrow('500');
		expect(comments.draft?.body).toBe('Too dark, and the sky is flat');
		expect(keptCommentDrafts.get(artifact === 'image' ? imageKey : 'image:doc:a.png')?.body).toBe('Too dark, and the sky is flat');
	});
}
