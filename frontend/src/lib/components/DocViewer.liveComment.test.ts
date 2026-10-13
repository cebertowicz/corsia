import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { EditorView } from '@codemirror/view';
import * as Y from 'yjs';
import DocViewer from './DocViewer.svelte';
import { connectionState } from '$lib/stores/connection';
import { emit } from '$lib/ws/events';
import { encodeUpdate, textDigest } from '$lib/stores/documentWire';
import { startLiveDocuments } from '$lib/stores/liveDocuments';
import { clearDraft } from '$lib/stores/docSaveQueue';
import { commentDraft, discardCommentDraft } from '$lib/stores/commentDraft';
import { RESERVE_BUDGET_MS } from '$lib/stores/commentSelection';
import { toasts } from '$lib/stores/toast';
import { docComments } from '$lib/stores/docComments';
const api = vi.hoisted(() => ({ fetchContent: vi.fn(), fetchRevision: vi.fn(), fetchComments: vi.fn(), postComment: vi.fn() }));
vi.mock('$lib/api/docs', async original => ({ ...await original<typeof import('$lib/api/docs')>(), ...api }));
const wire = vi.hoisted(() => ({ frames: [] as Record<string, any>[] }));
vi.mock('$lib/ws/state', () => ({ ws: { readyState: 1, send: (text: string) => wire.frames.push(JSON.parse(text)) } }));
vi.mock('$lib/stores/deviceDocument', () => ({ DeviceDocument: class {
	ready = Promise.resolve(); saved = true; error = ''; destroy = async () => {};
	subscribe(listener: (copy: unknown) => void) { listener(this); return () => {}; }
} }));
vi.mock('$lib/stores/deviceRecovery', () => ({ readDeviceRecovery: async () => [] }));
const target = { instanceId: 'live-comment-viewer', path: '/documents/doc.md', name: 'doc.md', projectId: 'project', threadId: 'thread', artifactId: 'artifact' };
let host: HTMLElement, component: ReturnType<typeof mount> | undefined, stop: () => void, server: Y.Doc;
beforeEach(() => {
	vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
	vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ open_count: 0, undelivered_batches: [], reviews: [], reviewers: [] }), { status: 200 })));
	HTMLElement.prototype.scrollIntoView = vi.fn();
	Range.prototype.getClientRects = vi.fn(() => [] as unknown as DOMRectList);
	Range.prototype.getBoundingClientRect = vi.fn(() => new DOMRect());
	vi.spyOn(EditorView.prototype, 'coordsAtPos').mockReturnValue(null);
	vi.spyOn(toasts, 'show').mockReturnValue('refusal');
	vi.spyOn(console, 'error').mockImplementation(() => {});
	connectionState.set('connected'); stop = startLiveDocuments();
	api.fetchContent.mockResolvedValue({ text: 'disk copy', writable: true, artifact: { id: 'artifact' }, freshness: null });
	api.fetchRevision.mockResolvedValue({ exists: true, writable: true, artifact_id: 'artifact', freshness: null });
	api.fetchComments.mockResolvedValue({ comments: [], feedback: { open_count: 0, owner_label: null, grace_closes_at: null, undelivered_batches: [] } });
	api.postComment.mockImplementation(async (_artifact, fields) => ({ id: fields.client_id, artifact_id: 'artifact',
		body: fields.body, anchor_text: fields.anchor_text, from_off: fields.from_off, to_off: fields.to_off,
		author: 'you', author_kind: 'user', created_at: '2026-10-07T00:00:00Z', parent_id: null, review: false, resolved: false, acked: false, replies: [] }));
});
afterEach(async () => {
	vi.useRealTimers();
	if (component) await unmount(component); component = undefined;
	emit('doc_ack', { type: 'doc_ack', artifact_id: 'artifact', store_id: 'store', message_ids: wire.frames.flatMap(frame => frame.message_id ? [frame.message_id] : []) });
	for (const frame of wire.frames.filter(frame => frame.type === 'doc_flush')) emit('doc_saved', {
		type: 'doc_saved', artifact_id: 'artifact', store_id: 'store', request_id: frame.request_id, server_saved: true, pending: false, held: null });
	await Promise.resolve(); await Promise.resolve();
	host?.remove(); stop(); server?.destroy(); clearDraft(target); discardCommentDraft(); docComments.set({}); wire.frames.length = 0;
	vi.restoreAllMocks(); vi.unstubAllGlobals(); for (const fn of Object.values(api)) fn.mockReset();
});
async function open() {
	host = document.createElement('div'); document.body.appendChild(host);
	component = mount(DocViewer, { target: host, props: { target } }); flushSync();
	await vi.waitFor(() => expect(wire.frames.some(frame => frame.type === 'doc_open')).toBe(true));
	const ask = wire.frames.findLast(frame => frame.type === 'doc_open')!;
	server = new Y.Doc(); server.getText('text').insert(0, 'alpha beta gamma');
	emit('doc_opened', { type: 'doc_opened', live: true, writable: true, artifact_id: 'artifact', store_id: 'store',
		request_id: ask.request_id, update: encodeUpdate(Y.encodeStateAsUpdate(server)), vector: encodeUpdate(Y.encodeStateVector(server)), pending: false, held: null });
	await vi.waitFor(() => expect(host.querySelector('.cm-content')?.textContent).toBe('alpha beta gamma'));
	emit('doc_ack', { type: 'doc_ack', artifact_id: 'artifact', store_id: 'store', message_ids: wire.frames.flatMap(frame => frame.message_id ? [frame.message_id] : []) });
}
async function select(from = 6, to = 10) {
	EditorView.findFromDOM(host.querySelector<HTMLElement>('.cm-editor')!)!.dispatch({ selection: { anchor: from, head: to } }); flushSync();
	await vi.waitFor(() => expect(host.querySelector('.sel-comment')).not.toBeNull());
}
async function compose(body = 'Keep my body') {
	host.querySelector<HTMLButtonElement>('.sel-comment')!.click(); flushSync();
	await vi.waitFor(() => expect(document.querySelector('.composer textarea')).not.toBeNull());
	const input = document.querySelector<HTMLTextAreaElement>('.composer textarea')!;
	input.value = body; input.dispatchEvent(new Event('input', { bubbles: true })); flushSync();
}
function remote(edit: (text: Y.Text) => void) {
	const vector = Y.encodeStateVector(server); edit(server.getText('text'));
	emit('doc_update', { type: 'doc_update', artifact_id: 'artifact', store_id: 'store', update: encodeUpdate(Y.encodeStateAsUpdate(server, vector)), vector: encodeUpdate(Y.encodeStateVector(server)) }); flushSync();
}
async function answer(ask: Record<string, any>, fields = {}) {
	emit('doc_positions', { type: 'doc_positions', artifact_id: 'artifact', store_id: 'store', request_id: ask.request_id, seq: ask.seq,
		reserved: true, ...fields });
}
const released = () => wire.frames.filter(frame => frame.type === 'doc_release');
it('captures original selection before Comment and submits its reserved range after remote prefix', async () => {
	await open(); await select(); remote(text => text.insert(0, 'NEW ')); await compose();
	document.querySelector<HTMLButtonElement>('.composer .submit')!.click();
	await vi.waitFor(() => expect(wire.frames.some(frame => frame.type === 'doc_positions')).toBe(true));
	const ask = wire.frames.findLast(frame => frame.type === 'doc_positions')!;
	await answer(ask, { request_id: 'unrelated' });
	expect(api.postComment).not.toHaveBeenCalled();
	await answer(ask);
	await vi.waitFor(() => expect(api.postComment).toHaveBeenCalledOnce());
	expect(ask.reserve).toMatchObject({ from_off: 10, to_off: 14, vector: encodeUpdate(Y.encodeStateVector(server)),
		digest: await textDigest(server.getText('text').toString()), text_length_utf16: 20 });
	expect(api.postComment.mock.calls[0][1]).toMatchObject({ client_id: ask.reserve.client_id, from_off: 10, to_off: 14, anchor_text: 'beta',
		body: 'Keep my body', live_capture: true });
	expect(released()).toEqual([]);
	await vi.waitFor(() => expect(document.querySelector('.composer')).toBeNull());
});
it('retains body across outside-close/reselection, rejects deleted identity and Cancel discards', async () => {
	await open(); await select(); await compose();
	remote(text => { text.delete(6, 4); text.insert(6, 'beta'); });
	const submit = document.querySelector<HTMLButtonElement>('.composer .submit')!;
	await vi.waitFor(() => expect(submit.disabled).toBe(true));
	const reason = submit.getAttribute('aria-describedby');
	expect(document.getElementById(reason!)?.textContent).toContain('your draft will carry over');
	document.querySelector<HTMLElement>('.drawer-backdrop')!.click(); flushSync();
	await vi.waitFor(() => expect(document.querySelector('.composer')).toBeNull());
	expect(get(commentDraft)?.body).toBe('Keep my body');
	await select(0, 5); host.querySelector<HTMLButtonElement>('.sel-comment')!.click(); flushSync();
	await vi.waitFor(() => expect(document.querySelector<HTMLTextAreaElement>('.composer textarea')?.value).toBe('Keep my body'));
	expect(document.querySelector<HTMLButtonElement>('.composer .submit')!.disabled).toBe(false);
	expect(document.querySelector('.selection-reason')).toBeNull();
	document.querySelector<HTMLButtonElement>('.composer .cancel')!.click(); flushSync();
	expect(get(commentDraft)).toBeNull();
	await select(11, 16); host.querySelector<HTMLButtonElement>('.sel-comment')!.click(); flushSync();
	expect(document.querySelector<HTMLTextAreaElement>('.composer textarea')?.value).toBe('');
});
it('highlights the selected text only while its composer is open: a close keeps the draft but clears it and reopening restores it', async () => {
	const marked = () => [...host.querySelectorAll('.cm-comment-pending')].map(node => node.textContent).join('');
	await open(); await select(); await compose();
	expect(marked()).toBe('beta');
	document.querySelector<HTMLElement>('.drawer-backdrop')!.click(); flushSync();
	await vi.waitFor(() => expect(document.querySelector('.composer')).toBeNull());
	expect(get(commentDraft)?.body).toBe('Keep my body');
	expect(marked()).toBe('');
	await select(); host.querySelector<HTMLButtonElement>('.sel-comment')!.click(); flushSync();
	await vi.waitFor(() => expect(marked()).toBe('beta'));
	document.querySelector<HTMLButtonElement>('.composer .cancel')!.click(); flushSync();
	expect(marked()).toBe('');
});
it('keeps the open composer beside its text when an edit before it shifts the text', async () => {
	const width = window.innerWidth;
	window.innerWidth = 1400;
	try {
		// The canvas the card stays inside: jsdom measures every element as empty, which would leave the card nowhere to be.
		const measured = HTMLElement.prototype.getBoundingClientRect;
		vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
			return this.classList.contains('doc') ? new DOMRect(0, 0, 1400, 900) : measured.call(this);
		});
		// Each character sits two pixels lower than the one before it, so an edit ahead of the selection moves its row.
		vi.spyOn(EditorView.prototype, 'coordsAtPos').mockImplementation((pos) => ({ left: pos, right: pos + 4, top: 100 + pos * 2, bottom: 110 + pos * 2 }));
		await open(); await select(); await compose();
		const card = () => document.querySelector<HTMLElement>('[data-popover]')!;
		await vi.waitFor(() => expect(card().style.top).toBe('112px'));
		remote(text => text.insert(0, 'XXXXXXXXXX'));
		await vi.waitFor(() => expect(host.querySelector('.cm-comment-pending')?.textContent).toBe('beta'));
		await vi.waitFor(() => expect(card().style.top).toBe('132px'));
	} finally {
		window.innerWidth = width;
	}
});
it('cancels the positions wait on outside close and ignores its late reply', async () => {
	await open(); await select(); await compose();
	document.querySelector<HTMLButtonElement>('.composer .submit')!.click();
	await vi.waitFor(() => expect(wire.frames.some(frame => frame.type === 'doc_positions')).toBe(true));
	const ask = wire.frames.findLast(frame => frame.type === 'doc_positions')!;
	document.querySelector<HTMLElement>('.drawer-backdrop')!.click(); flushSync();
	await vi.waitFor(() => expect(document.querySelector('.composer')).toBeNull());
	await answer(ask);
	await Promise.resolve(); await Promise.resolve();
	expect(api.postComment).not.toHaveBeenCalled();
	expect(released()).toEqual([expect.objectContaining({ client_id: ask.reserve.client_id })]);
	expect(get(commentDraft)?.body).toBe('Keep my body');
});
it.each([
	['a store mismatch', { kind: 'store_mismatch', store_id: 'other' }],
	['a document-not-open error', { kind: 'not_open' }],
	['a frame-handler refusal', { kind: 'refused', store_id: 'store', message: 'refused for test' }],
])('keeps the draft and re-enables Comment without posting when a reservation reports %s', async (_name, fields) => {
	await open(); await select(); await compose();
	const submit = document.querySelector<HTMLButtonElement>('.composer .submit')!;
	submit.click();
	await vi.waitFor(() => expect(wire.frames.some(frame => frame.type === 'doc_positions')).toBe(true));
	const ask = wire.frames.findLast(frame => frame.type === 'doc_positions')!;
	emit('doc_error', { type: 'doc_error', artifact_id: 'artifact', request_id: ask.request_id, ...fields });
	await vi.waitFor(() => expect(submit.disabled).toBe(false));
	expect(submit.textContent).toBe('Comment');
	expect(document.querySelector<HTMLTextAreaElement>('.composer textarea')?.value).toBe('Keep my body');
	expect(get(commentDraft)?.body).toBe('Keep my body');
	expect(api.postComment).not.toHaveBeenCalled();
	expect(released()).toEqual([expect.objectContaining({ client_id: ask.reserve.client_id })]);
});
it('after a store mismatch, reopening the composer and submitting sends one reservation and one post carrying the draft', async () => {
	await open(); await select(); await compose();
	document.querySelector<HTMLButtonElement>('.composer .submit')!.click();
	await vi.waitFor(() => expect(wire.frames.some(frame => frame.type === 'doc_positions')).toBe(true));
	const refused = wire.frames.findLast(frame => frame.type === 'doc_positions')!;
	emit('doc_error', { type: 'doc_error', artifact_id: 'artifact', store_id: 'other', request_id: refused.request_id, kind: 'store_mismatch' });
	await vi.waitFor(() => expect(document.querySelector<HTMLButtonElement>('.composer .submit')!.disabled).toBe(false));
	expect(api.postComment).not.toHaveBeenCalled();
	document.querySelector<HTMLElement>('.drawer-backdrop')!.click(); flushSync();
	await vi.waitFor(() => expect(document.querySelector('.composer')).toBeNull());
	await select(); host.querySelector<HTMLButtonElement>('.sel-comment')!.click(); flushSync();
	await vi.waitFor(() => expect(document.querySelector<HTMLTextAreaElement>('.composer textarea')?.value).toBe('Keep my body'));
	const asks = () => wire.frames.filter(frame => frame.type === 'doc_positions');
	expect(asks()).toHaveLength(1);
	document.querySelector<HTMLButtonElement>('.composer .submit')!.click();
	await vi.waitFor(() => expect(asks()).toHaveLength(2));
	const retry = asks()[1];
	expect(retry.request_id).not.toBe(refused.request_id);
	await answer(retry);
	await vi.waitFor(() => expect(api.postComment).toHaveBeenCalledOnce());
	expect(api.postComment.mock.calls[0][1]).toMatchObject({ client_id: retry.reserve.client_id, anchor_text: 'beta', body: 'Keep my body', live_capture: true });
	expect(retry.reserve.client_id).not.toBe(refused.reserve.client_id);
	await vi.waitFor(() => expect(document.querySelector('.composer')).toBeNull());
	expect(asks()).toHaveLength(2);
	expect(api.postComment).toHaveBeenCalledOnce();
	expect(released()).toEqual([expect.objectContaining({ client_id: refused.reserve.client_id })]);
});
it('keeps the draft and re-enables Comment without posting when a reservation reply times out', async () => {
	await open(); await select(); await compose();
	vi.useFakeTimers();
	const submit = document.querySelector<HTMLButtonElement>('.composer .submit')!;
	submit.click();
	await vi.waitFor(() => expect(wire.frames.some(frame => frame.type === 'doc_positions')).toBe(true));
	const ask = wire.frames.findLast(frame => frame.type === 'doc_positions')!;
	await vi.advanceTimersByTimeAsync(RESERVE_BUDGET_MS);
	await vi.waitFor(() => expect(submit.disabled).toBe(false));
	expect(submit.textContent).toBe('Comment');
	expect(document.querySelector<HTMLTextAreaElement>('.composer textarea')?.value).toBe('Keep my body');
	expect(get(commentDraft)?.body).toBe('Keep my body');
	expect(api.postComment).not.toHaveBeenCalled();
	expect(released()).toEqual([expect.objectContaining({ client_id: ask.reserve.client_id })]);
});
it('unmount cancels its capture, discards its scoped draft and ignores late answers', async () => {
	await open(); await select(); await compose();
	document.querySelector<HTMLButtonElement>('.composer .submit')!.click();
	await vi.waitFor(() => expect(wire.frames.some(frame => frame.type === 'doc_positions')).toBe(true));
	const ask = wire.frames.findLast(frame => frame.type === 'doc_positions')!;
	const request = get(commentDraft)!.request!;
	await unmount(component!); component = undefined;
	expect(request.signal.aborted).toBe(true);
	expect(get(commentDraft)).toBeNull();
	await answer(ask);
	expect(api.postComment).not.toHaveBeenCalled();
	expect(released()).toEqual([expect.objectContaining({ client_id: ask.reserve.client_id })]);
});
it('a sibling viewer loading and unmounting cannot discard another viewer’s draft', async () => {
	await open(); await select(); await compose();
	const draft = get(commentDraft), siblingHost = document.createElement('div'); document.body.appendChild(siblingHost);
	const siblingTarget = { ...target, instanceId: 'sibling-viewer' };
	const openingCount = wire.frames.filter(frame => frame.type === 'doc_open').length;
	const sibling = mount(DocViewer, { target: siblingHost, props: { target: siblingTarget } }); flushSync();
	try {
		await vi.waitFor(() => expect(wire.frames.filter(frame => frame.type === 'doc_open')).toHaveLength(openingCount + 1));
		expect(get(commentDraft)?.identity).toBe(draft!.identity);
		const ask = wire.frames.findLast(frame => frame.type === 'doc_open')!;
		emit('doc_opened', { type: 'doc_opened', live: true, writable: true, artifact_id: 'artifact', store_id: 'store',
			request_id: ask.request_id, update: encodeUpdate(Y.encodeStateAsUpdate(server)), vector: encodeUpdate(Y.encodeStateVector(server)), pending: false, held: null });
		await vi.waitFor(() => expect(siblingHost.querySelector('.cm-content')).not.toBeNull());
		expect(get(commentDraft)?.identity).toBe(draft!.identity);
		expect(get(commentDraft)?.body).toBe('Keep my body');
	} finally {
		await unmount(sibling); siblingHost.remove(); clearDraft(siblingTarget);
	}
	expect(get(commentDraft)?.identity).toBe(draft!.identity);
});
const TRY_AGAIN = "Couldn't post the comment — try again";
const postFailure = (name: string) => Object.assign(new Error('request timed out after 12000ms (may have committed server-side)'), { name });
it.each([
	['a server error', new Error('500 synthetic comment failure'), TRY_AGAIN],
	['a gateway error', new Error('502 <html>Bad gateway</html>'), TRY_AGAIN],
	['a timeout', postFailure('PostTimeoutError'), "We couldn't confirm your comment was posted. Check the comments before posting again. Your draft is kept."],
	['a refusal with a reason', new Error('404 {"detail":"This document is no longer shared."}'), "Couldn't post the comment: This document is no longer shared. Your draft is kept."],
	['a refusal whose reason is a machine code', new Error('400 {"detail":"live_capture_refused"}'), "Couldn't post the comment. Your draft is kept."],
	['a refusal with a non-JSON body', new Error('400 <html>Bad request</html>'), "Couldn't post the comment. Your draft is kept."],
	['a refusal whose body has no reason', new Error('422 {"detail":[{"msg":"bad"}]}'), "Couldn't post the comment. Your draft is kept."],
])('tells the user a live comment failed in plain words after %s, and keeps the draft', async (_name, failure, message) => {
	await open(); await select(); await compose();
	api.postComment.mockRejectedValueOnce(failure);
	const submit = document.querySelector<HTMLButtonElement>('.composer .submit')!;
	submit.click();
	await vi.waitFor(() => expect(wire.frames.some(frame => frame.type === 'doc_positions')).toBe(true));
	await answer(wire.frames.findLast(frame => frame.type === 'doc_positions')!);
	await vi.waitFor(() => expect(submit.disabled).toBe(false));
	expect(toasts.show).toHaveBeenCalledWith(message, 'error');
	// Never wire text; and only a failure a retry can fix invites one.
	expect(toasts.show).not.toHaveBeenCalledWith(expect.stringMatching(/^\d{3} |timed out|<html>/), 'error');
	if (message !== TRY_AGAIN) expect(toasts.show).not.toHaveBeenCalledWith(expect.stringMatching(/try again/i), 'error');
	expect(document.querySelector<HTMLTextAreaElement>('.composer textarea')?.value).toBe('Keep my body');
	expect(get(commentDraft)?.body).toBe('Keep my body');
});
it('logs a refused live comment post\'s status and raw reason once, as the shared error gate does', async () => {
	await open(); await select(); await compose();
	const logged = vi.spyOn(console, 'error').mockImplementation(() => {});
	api.postComment.mockRejectedValueOnce(new Error('400 {"detail":"live_capture_refused"}'));
	const submit = document.querySelector<HTMLButtonElement>('.composer .submit')!;
	submit.click();
	await vi.waitFor(() => expect(wire.frames.some(frame => frame.type === 'doc_positions')).toBe(true));
	await answer(wire.frames.findLast(frame => frame.type === 'doc_positions')!);
	await vi.waitFor(() => expect(submit.disabled).toBe(false));
	expect(logged.mock.calls.filter(call => call[0] === '[api] request rejected')).toEqual([['[api] request rejected', 400, 'live_capture_refused']]);
	logged.mockRestore();
});
it('keeps the body and enabled Comment after HTTP refusal, then dismisses its toast on retry success', async () => {
	await open(); await select(); await compose();
	api.postComment.mockRejectedValueOnce(new Error('409 {"detail":"The document changed. Try Comment again; your draft is kept."}'));
	const dismiss = vi.spyOn(toasts, 'dismiss');
	const submit = document.querySelector<HTMLButtonElement>('.composer .submit')!;
	submit.click();
	await vi.waitFor(() => expect(wire.frames.some(frame => frame.type === 'doc_positions')).toBe(true));
	const refusedAsk = wire.frames.findLast(frame => frame.type === 'doc_positions')!;
	await answer(refusedAsk);
	await vi.waitFor(() => expect(submit.disabled).toBe(false));
	expect(released()).toEqual([expect.objectContaining({ client_id: refusedAsk.reserve.client_id })]);
	expect(submit.textContent).toBe('Comment');
	expect(document.querySelector<HTMLTextAreaElement>('.composer textarea')?.value).toBe('Keep my body');
	expect(get(commentDraft)?.body).toBe('Keep my body');
	expect(toasts.show).toHaveBeenCalledWith('The document changed before your comment could be attached. Your draft is kept. Try again.', 'error');
	const count = wire.frames.filter(frame => frame.type === 'doc_positions').length;
	submit.click();
	await vi.waitFor(() => expect(wire.frames.filter(frame => frame.type === 'doc_positions')).toHaveLength(count + 1));
	await answer(wire.frames.findLast(frame => frame.type === 'doc_positions')!);
	await vi.waitFor(() => expect(document.querySelector('.composer')).toBeNull());
	expect(dismiss).toHaveBeenCalledWith('refusal');
	expect(get(commentDraft)).toBeNull();
});
