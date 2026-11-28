// A reply the person pressed Send on that goes on its own (`unsentComments`) says so under its field and holds Send;
// its field stays editable, and an edit makes it a typed reply again. Its own press looks as it always did.
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { flushSync } from 'svelte';
import { createClassComponent } from 'svelte/legacy';
import CommentList from './CommentList.svelte';
import { createMentionSelection } from './mentionSelection';
import { documentDraftKey, keptCommentDrafts, replyDraftKey } from '$lib/stores/keptCommentDrafts.svelte';
import { unsentComments } from '$lib/stores/unsentComments.svelte';
import { user } from '$lib/stores/auth';
const api = vi.hoisted(() => ({ postComment: vi.fn() }));
vi.mock('$lib/api/docs', async original => ({ ...await original<typeof import('$lib/api/docs')>(), ...api }));

const scope = documentDraftKey({ projectId: 'project', path: '/documents/doc.md' });
const key = replyDraftKey(scope, 'c1');
const comment = { id: 'c1', anchor: 'quote', author: 'you', text: 'note', at: 'now', resolved: false, replies: [] };
let host: HTMLElement;
let component: ReturnType<typeof createClassComponent> | null = null;

/** The reply was pressed and its post did not go: it is kept with what its post needs, so it goes on its own. */
function pressedReply(text: string) {
	keptCommentDrafts.keepReply(key, text, createMentionSelection(text));
	keptCommentDrafts.claim(key, text, createMentionSelection(text), undefined, { artifactId: 'artifact', threadId: null });
}
function render(onReply = vi.fn().mockResolvedValue(undefined)) {
	host = document.createElement('div'); document.body.appendChild(host);
	component = createClassComponent({ target: host, component: CommentList, props: { comments: [comment], documentOrder: true, draftScope: scope, onReply } });
	flushSync();
	return onReply;
}
const field = () => host.querySelector<HTMLTextAreaElement>('.ct-input')!;
const send = () => host.querySelector<HTMLButtonElement>('.ct-send-slot button')!;
const line = () => host.querySelector('.ct-reply-sending');
function type(text: string) { field().value = text; field().dispatchEvent(new Event('input', { bubbles: true })); flushSync(); }

beforeEach(() => { user.set({ id: 'person-1', name: 'person-1' } as never); });
afterEach(async () => {
	await unsentComments.settled();
	component?.$destroy(); component = null; host?.remove();
	keptCommentDrafts.reset(); unsentComments.reset(); user.set(null); api.postComment.mockReset();
});

it('a pressed reply going on its own: a muted "Sending…" line, Send held, and its field still editable', () => {
	pressedReply('Agreed');
	const onReply = render();
	expect(line()?.textContent).toBe('Sending…');
	expect(line()?.getAttribute('role')).toBe('status');
	expect(host.querySelector('.ct-reply-error')).toBeNull();
	expect(send().disabled).toBe(true);
	expect(send().getAttribute('aria-label')).toBe('Sending…');
	expect(field().readOnly).toBe(false);
	// Enter and a click on Send do nothing while it goes on its own.
	field().dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true })); flushSync();
	send().click(); flushSync();
	expect(onReply).not.toHaveBeenCalled();
});

it('typing in a "Sending…" reply makes it a typed reply: Send is back, the line goes, and nothing goes on its own', async () => {
	pressedReply('Agreed');
	const onReply = render();
	expect(line()?.textContent).toBe('Sending…');
	type('Agreed, with one change');
	expect(line()).toBeNull();
	expect(send().disabled).toBe(false);
	expect(send().getAttribute('aria-label')).toBe('Send reply');
	expect(keptCommentDrafts.pressed()).toEqual([]);
	unsentComments.open(key);
	await unsentComments.settled();
	expect(api.postComment).not.toHaveBeenCalled();
	send().click(); flushSync();
	expect(onReply).toHaveBeenCalledWith('c1', 'Agreed, with one change', [], [], key);
});

it('the reply\'s own press reads as it always did: no "Sending…" line and Send not held, while that press is out', async () => {
	let release!: () => void;
	// The press claims the draft with what its post needs, as the viewer's reply does, and its post stays out.
	const onReply = vi.fn((_id: string, text: string) => {
		keptCommentDrafts.claim(key, text, createMentionSelection(text), undefined, { artifactId: 'artifact', threadId: null });
		return new Promise<void>((resolve) => { release = resolve; });
	});
	render(onReply);
	type('Agreed');
	send().click(); flushSync();
	expect(onReply).toHaveBeenCalledOnce();
	expect(unsentComments.sending(key)).toBe(true);
	expect(line()).toBeNull();
	expect(send().getAttribute('aria-label')).toBe('Send reply');
	release(); await Promise.resolve(); flushSync();
});
