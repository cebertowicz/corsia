import { afterEach, expect, it, vi } from 'vitest';
import { mount, unmount, flushSync } from 'svelte';
import * as Y from 'yjs';
import DocViewerStatus from './DocViewerStatus.svelte';
import { LiveDocumentSession } from '$lib/stores/liveDocumentSession';

let component: ReturnType<typeof mount> | null;
let host: HTMLElement;
let session: LiveDocumentSession;
const copy = vi.fn().mockResolvedValue(undefined);
afterEach(async () => {
	if (component) await unmount(component);
	host?.remove(); session?.destroy(); session?.doc.destroy();
	vi.restoreAllMocks(); vi.unstubAllGlobals(); copy.mockClear();
});
function setup(send: (frame: Record<string, unknown>) => boolean = () => true) {
	vi.stubGlobal('navigator', { clipboard: { writeText: copy } });
	const doc = new Y.Doc(); doc.getText('text').insert(0, 'live text');
	session = new LiveDocumentSession({ doc, artifactId: 'artifact', storeId: 'store', writable: true, send });
	session.connection = 'connected';
}
function render(overrides: Record<string, unknown> = {}) {
	host = document.createElement('div'); document.body.appendChild(host);
	component = mount(DocViewerStatus, { target: host, props: {
		liveSession: session, ready: true, stale: false, readonly: false, commentCount: 0,
		orphanedCount: 0, artifactId: null, targetInstanceId: 'tab', freshness: null, onOpenComments: () => {},
		...overrides,
	} }); flushSync();
}
// The recovery drawer is portaled to the body, after the host, so the last match is the drawer's own control, never a closing dialog's.
const button = (text: string) => [...document.querySelectorAll('button')].filter(button => button.textContent?.trim() === text).at(-1)!;
const recoveryButton = (text: string) => [...document.body.querySelectorAll<HTMLButtonElement>('[data-adaptive-drawer] button, .popover button')].find(button => button.textContent?.trim() === text)!;

it('keeps the connected line quiet and reports a failed device copy while disconnected', async () => {
	setup(); render(); expect(host.textContent).not.toContain('Disconnected');
	session.deviceError = 'quota refused'; session.disconnected(); flushSync();
	expect(host.textContent).toContain("Disconnected · changes aren't saved; keep this tab open");
	button('Copy text').click(); await vi.waitFor(() => expect(copy).toHaveBeenCalledWith('live text'));
});
it('reports a successfully persisted disconnected copy and same-store reconnection', () => {
	setup(); session.attachDevice({ saved: true, subscribe: () => () => {} } as any, []);
	session.disconnected(); render();
	expect(host.textContent).toContain('Disconnected · changes stay on this device');
	session.reconnecting(); flushSync(); expect(host.textContent).toContain('Reconnecting…');
});
it('names live unplaced comments once on the document status pill', () => {
	setup(); render({ orphanedCount: 2 });
	const pill = host.querySelector('.comment-status')!;
	expect(pill.getAttribute('aria-label')).toBe("0 open threads, 2 comments couldn't be placed");
});

it('shows the approved settling row and offers reading and copying without a second write', async () => {
	setup(); session.pending = true;
	const retry = vi.spyOn(session, 'retry'); const flush = vi.spyOn(session, 'flush');
	render(); expect(host.textContent).toContain('This write is still settling. The file may already have changed; saving on the server is not confirmed.');
	button('Details').click(); flushSync();
	expect(document.body.textContent).toContain('Read the document before writing again.');
	button('Copy text').click(); await vi.waitFor(() => expect(copy).toHaveBeenCalledWith('live text'));
	expect(retry).not.toHaveBeenCalled(); expect(flush).not.toHaveBeenCalled(); expect(button('Retry')).toBeUndefined();
});
it('retries a server-store refusal explicitly and keeps the row after a refused retry', async () => {
	setup(); session.storeError = 'disk full'; const retry = vi.spyOn(session, 'retry').mockRejectedValue(new Error('still full'));
	render(); expect(host.textContent).toContain('Not saved on the server: disk full');
	button('Retry').click(); await vi.waitFor(() => expect(document.body.textContent).toContain('still full'));
	expect(retry).toHaveBeenCalledOnce(); expect(session.storeError).toBe('disk full');
});
it('keeps both held texts and candidate copyable and asks before each choice', async () => {
	setup(); session.held = { corsia_text: 'kept CORSIA', file_text: 'outside file', candidate_text: 'candidate', choice_pending: false };
	const choose = vi.spyOn(session, 'choose').mockResolvedValue({ type: 'doc_saved' }); render();
	expect(host.textContent).toContain('This file changed outside CORSIA while a write was being saved.');
	button('Details').click(); flushSync();
	for (const [label, text] of [['Copy CORSIA text', 'kept CORSIA'], ['Copy file text', 'outside file'], ['Copy interrupted text', 'candidate']]) {
		button(label).click(); await vi.waitFor(() => expect(copy).toHaveBeenCalledWith(text));
	}
	for (const [label, choice] of [['Keep CORSIA text, overwrite the file', 'corsia'], ["Use the file's text, replace CORSIA text", 'file']]) {
		await vi.waitFor(() => expect(button(label).disabled).toBe(false));
		recoveryButton(label).click(); flushSync(); expect(choose).not.toHaveBeenCalled();
		button('Cancel').click(); flushSync(); expect(session.held?.file_text).toBe('outside file');
		recoveryButton(label).click(); flushSync();
		const dialog = [...document.body.querySelectorAll('.modal[role="alertdialog"]')].at(-1)!;
		[...dialog.querySelectorAll('button')].find(item => item.textContent?.trim() === label)!.click();
		await vi.waitFor(() => expect(choose).toHaveBeenCalledWith(choice)); choose.mockClear();
	}
});
it('retains an old-store copy through a cancelled or refused replacement and never merges it automatically', async () => {
	setup(); session.recovery = [{ storeId: 'old', text: 'old local copy' }];
	const replace = vi.spyOn(session, 'replace').mockRejectedValue(new Error('replace refused')); render();
	expect(host.textContent).toContain('This document was reset on the server. Your unsaved text is on this device.');
	button('Details').click(); flushSync(); recoveryButton('Replace with my text').click(); flushSync();
	expect(replace).not.toHaveBeenCalled(); button('Cancel').click(); flushSync();
	expect(session.recovery[0].text).toBe('old local copy'); recoveryButton('Replace with my text').click(); flushSync();
	const dialog = [...document.body.querySelectorAll('.modal[role="alertdialog"]')].at(-1)!;
	[...dialog.querySelectorAll('button')].find(item => item.textContent?.trim() === 'Replace with my text')!.click();
	await vi.waitFor(() => expect(document.body.textContent).toContain('replace refused'));
	expect(session.text.toString()).toBe('live text'); expect(session.recovery[0].text).toBe('old local copy');
});

function blockers(pending = false) {
	session.admitFailures({ type: 'doc_opened', live: true, artifact_id: 'artifact', store_id: 'store', setup_incarnation: 'manager' });
	session.handle({ type: 'doc_error', artifact_id: 'artifact', store_id: 'store', kind: 'publication_failed',
		blocking: true, failure_generation: 3, message: '{"detail":"private fixture publication"}' });
	session.handle({ type: 'doc_error', artifact_id: 'artifact', store_id: 'store', kind: 'setup_failed',
		blocking: true, setup_incarnation: 'manager', setup_revision: 1, message: 'private fixture setup' });
	session.pending = pending;
}

it('keeps settling precedence and two neutral blockers with one disabled Retry and full text Copy', async () => {
	setup(); blockers(true);
	const retry = vi.spyOn(session, 'retry'), flush = vi.spyOn(session, 'flush');
	render();
	expect(host.textContent).toContain('This write is still settling.');
	expect(host.textContent).toContain("Live updates couldn't be shared for this document.");
	expect(host.textContent).toContain("Live editing couldn't start for this document.");
	expect(host.textContent).not.toContain('private fixture');
	expect(host.textContent).not.toContain('Not saved');
	const retries = [...host.querySelectorAll('button')].filter(item => item.textContent?.trim() === 'Retry');
	expect(retries).toHaveLength(1); expect(retries[0].disabled).toBe(true);
	retries[0].click(); expect(retry).not.toHaveBeenCalled(); expect(flush).not.toHaveBeenCalled();
	button('Copy text').click(); await vi.waitFor(() => expect(copy).toHaveBeenCalledWith('live text'));
});

it.each(['held', 'readonly'])('disables the single blocker Retry for %s without losing Copy', async state => {
	setup(); blockers();
	if (state === 'held') session.held = { corsia_text: 'live text', file_text: 'outside', candidate_text: 'candidate', choice_pending: false };
	else session.writable = false;
	const retry = vi.spyOn(session, 'retry'); render();
	expect(button('Retry').disabled).toBe(true); button('Retry').click(); expect(retry).not.toHaveBeenCalled();
	button('Copy text').click(); await vi.waitFor(() => expect(copy).toHaveBeenCalledWith('live text'));
});

it('bounds three blocker lines to two plus the remaining count', () => {
	setup(); blockers();
	session.handle({ type: 'doc_error', artifact_id: 'artifact', store_id: 'store', kind: 'store_refused', blocking: true, failure_generation: 4, message: 'disk fixture' });
	render(); expect(host.textContent).toContain('Not saved on the server: disk fixture');
	expect(host.textContent).toContain("Live updates couldn't be shared for this document.");
	expect(host.textContent).toContain('+1 more');
	expect(host.textContent).not.toContain("Live editing couldn't start for this document.");
});

it('uses enabled canonical Retry without leaking matched request reasons into the action row', async () => {
	const frames: Record<string, unknown>[] = [];
	setup(frame => { frames.push(frame); return true; }); blockers(); render();
	const retry = vi.spyOn(session, 'retry');
	expect(button('Retry').disabled).toBe(false);
	button('Retry').click(); await vi.waitFor(() => expect(retry).toHaveBeenCalledOnce());
	const request = frames.find(frame => frame.type === 'doc_flush')!;
	session.handle({ type: 'doc_error', artifact_id: 'artifact', store_id: 'store', request_id: request.request_id as string,
		kind: 'publication_failed', message: '{"detail":"private fixture request"}' });
	await vi.waitFor(() => expect(document.body.querySelector('[role="alert"]')?.textContent).toBe("Live updates couldn't be shared for this document."));
	expect(document.body.textContent).not.toContain('private fixture');
	button('Copy text').click(); await vi.waitFor(() => expect(copy).toHaveBeenCalledWith('live text'));
});
