import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, unmount, flushSync } from 'svelte';
import { createHash } from 'node:crypto';

const sha = (text: string) => createHash('sha256').update(text, 'utf8').digest('hex');
const mocks = vi.hoisted(() => ({ writeContent: vi.fn(), fetchContent: vi.fn(), flushPositions: vi.fn(), toastShow: vi.fn() }));
vi.mock('$lib/api/docs', () => ({ writeContent: mocks.writeContent, fetchContent: mocks.fetchContent }));
vi.mock('$lib/stores/docComments', () => ({ flushPositions: mocks.flushPositions }));
vi.mock('$lib/stores/toast', () => ({ toasts: { show: mocks.toastShow } }));

import DocStaleChoice from './DocStaleChoice.svelte';
import { clearDraft, existingDraft, flushDraft, seedDraft, setDraftText } from '$lib/stores/docSaveQueue';
import { EDITOR_STALE_STATUS } from '$lib/stores/docSaveBase';
import type { DocViewerTarget } from '$lib/docViewerTarget';

const staleRefusal = () => new Error(`409 ${JSON.stringify({ detail: { code: 'stale_base', message: 'Plain sentence.' } })}`);
const file = (text: string, revision = 'r-file') => ({ text, revision, size: text.length, mtime: 0, writable: true, artifact: null, freshness: null });

let host: HTMLElement;
let component: ReturnType<typeof mount> | null = null;
let doc: DocViewerTarget;
let seq = 0;
const onUseFile = vi.fn();
const copy = vi.fn().mockResolvedValue(undefined);

beforeEach(async () => {
	vi.stubGlobal('navigator', { clipboard: { writeText: copy } });
	vi.spyOn(console, 'error').mockImplementation(() => {});
	doc = { instanceId: `choice-tab-${++seq}`, path: '/project/note.md', name: 'note.md', projectId: 'p', threadId: 't', artifactId: 'a' };
	mocks.writeContent.mockRejectedValueOnce(staleRefusal());
	seedDraft(doc, 'base');
	setDraftText(doc, 'my typed text');
	expect(await flushDraft(doc)).toBe(false);
	host = document.createElement('div'); document.body.appendChild(host);
	component = mount(DocStaleChoice, { target: host, props: { tabKey: doc.instanceId, onUseFile } });
	flushSync();
});
afterEach(async () => {
	if (component) await unmount(component);
	component = null; host.remove(); document.body.innerHTML = '';
	clearDraft(doc);
	for (const mock of Object.values(mocks)) mock.mockReset();
	onUseFile.mockReset(); copy.mockClear();
	vi.restoreAllMocks(); vi.unstubAllGlobals();
});

const buttons = (text: string) => [...document.querySelectorAll('button')].filter((button) => button.textContent?.trim() === text);
const button = (text: string) => buttons(text).at(-1)!;
const dialog = () => document.querySelector<HTMLElement>('[role="alertdialog"]');
const confirm = (label: string) => [...dialog()!.querySelectorAll('button')].find((button) => button.textContent?.trim() === label)!.click();
async function openDetails() {
	button('Details').click();
	await vi.waitFor(() => expect(document.body.textContent).toContain('The file’s text'));
}

describe('DocStaleChoice', () => {
	it('shows both texts with a Copy for each and fetches the file only when Details opens', async () => {
		mocks.fetchContent.mockResolvedValue(file('what the file holds'));
		expect(mocks.fetchContent).not.toHaveBeenCalled();
		await openDetails();

		expect(mocks.fetchContent).toHaveBeenCalledWith('/project/note.md', { projectId: 'p', threadId: 't' });
		expect(document.body.textContent).toContain('my typed text');
		expect(document.body.textContent).toContain('what the file holds');
		button('Copy your text').click();
		button('Copy the file’s text').click();
		await vi.waitFor(() => expect(copy.mock.calls).toEqual([['my typed text'], ['what the file holds']]));
	});

	it('closes the phone sheet with its heading’s Done and keeps Close only on the popover', async () => {
		mocks.fetchContent.mockResolvedValue(file('what the file holds'));
		const width = window.innerWidth;
		try {
			window.innerWidth = 600;
			await openDetails();
			expect(buttons('Done')).toHaveLength(1);
			expect(buttons('Close')).toHaveLength(0);
			button('Done').click();
			flushSync();
			expect(document.body.textContent).not.toContain('The file’s text');
			window.innerWidth = 1440;
			await openDetails();
			expect(buttons('Done')).toHaveLength(0);
			expect(buttons('Close')).toHaveLength(1);
		} finally { window.innerWidth = width; }
	});

	it('draws the choices as the bordered secondary button, not as quiet text', async () => {
		mocks.fetchContent.mockResolvedValue(file('what the file holds'));
		await openDetails();
		for (const label of ['Keep your text', 'Use the file’s text']) expect(button(label).classList.contains('secondary')).toBe(true);
		for (const label of ['Copy your text', 'Copy the file’s text']) expect(button(label).classList.contains('ghost')).toBe(true);
	});

	it('offers Retry when the file cannot be read, keeps his text, and shows both texts once it can', async () => {
		mocks.fetchContent.mockRejectedValueOnce(new TypeError('Failed to fetch')).mockResolvedValueOnce(file('the file, read on retry'));
		button('Details').click();
		await vi.waitFor(() => expect(document.body.textContent).toContain('Couldn’t load the file’s text. Your text stays here.'));
		expect(buttons('Keep your text')).toHaveLength(0);
		expect(buttons('Use the file’s text')).toHaveLength(0);
		expect(existingDraft(doc)).toMatchObject({ editText: 'my typed text', stale: true });

		button('Retry').click();
		await vi.waitFor(() => expect(document.body.textContent).toContain('the file, read on retry'));
		expect(existingDraft(doc)?.editText).toBe('my typed text');
	});

	it('never replaces his text before the confirmation, and not when it is cancelled', async () => {
		mocks.fetchContent.mockResolvedValue(file('what the file holds'));
		await openDetails();
		button('Use the file’s text').click();
		flushSync();

		expect(dialog()?.textContent).toContain('This replaces your text with the file’s text.');
		expect(onUseFile).not.toHaveBeenCalled();
		expect(existingDraft(doc)?.editText).toBe('my typed text');
		button('Cancel').click();
		await vi.waitFor(() => expect(dialog()).toBeNull());
		expect(onUseFile).not.toHaveBeenCalled();
		expect(existingDraft(doc)).toMatchObject({ editText: 'my typed text', stale: true, status: 'error' });
	});

	it('Use the file’s text, once confirmed, hands the file to the viewer’s content writer and ends the refusal', async () => {
		mocks.fetchContent.mockResolvedValue(file('what the file holds', 'r7'));
		await openDetails();
		button('Use the file’s text').click();
		flushSync();
		confirm('Use the file’s text');

		await vi.waitFor(() => expect(onUseFile).toHaveBeenCalledTimes(1));
		expect(onUseFile.mock.calls[0][0]).toMatchObject({ text: 'what the file holds', artifactId: 'a', revision: 'r7' });
		expect(existingDraft(doc)).toMatchObject({ editText: 'what the file holds', savedText: 'what the file holds', status: 'idle', stale: false, error: '' });
	});

	it('Keep your text, once confirmed, saves his text against the digest of the file text it showed', async () => {
		mocks.fetchContent.mockResolvedValue(file('what the file holds'));
		mocks.writeContent.mockResolvedValueOnce({ saved: true, revision: 'r8' });
		await openDetails();
		button('Keep your text').click();
		flushSync();
		expect(dialog()?.textContent).toContain('This replaces the file’s text with yours.');
		confirm('Keep your text');

		await vi.waitFor(() => expect(existingDraft(doc)).toMatchObject({ savedText: 'my typed text', stale: false, error: '' }));
		const call = mocks.writeContent.mock.calls.at(-1)!;
		expect(call[1]).toBe('my typed text');
		expect((call[2] as { expectedSha256: string }).expectedSha256).toBe(sha('what the file holds'));
		expect(onUseFile).not.toHaveBeenCalled();
	});

	it('says its own sentence when Keep your text fails with no detail, and the refusal’s own line when it has one', async () => {
		mocks.fetchContent.mockResolvedValue(file('what the file holds'));
		mocks.writeContent.mockRejectedValueOnce(new TypeError('Failed to fetch'));
		await openDetails();
		button('Keep your text').click();
		flushSync();
		confirm('Keep your text');
		await vi.waitFor(() => expect(document.body.textContent).toContain('Couldn’t save the document. Your text stays here. Choose Keep your text to try again.'));
		expect(document.body.textContent).not.toContain('edits are still local');
		expect(existingDraft(doc)).toMatchObject({ editText: 'my typed text', stale: true, status: 'error' });

		const deleted = 'Couldn’t save because this document is deleted. Your text stays here until you close it.';
		mocks.writeContent.mockRejectedValueOnce(new Error(`404 ${JSON.stringify({ detail: deleted })}`));
		button('Keep your text').click();
		flushSync();
		confirm('Keep your text');
		await vi.waitFor(() => expect(document.body.textContent).toContain(deleted));
		expect(document.body.textContent).not.toContain('Choose Keep your text to try again.');
		expect(existingDraft(doc)).toMatchObject({ editText: 'my typed text', stale: true, error: deleted });
	});

	it('refreshes Details and keeps his text when the file changed again', async () => {
		mocks.fetchContent.mockResolvedValueOnce(file('file, first version')).mockResolvedValueOnce(file('file, second version'));
		mocks.writeContent.mockRejectedValueOnce(staleRefusal());
		await openDetails();
		button('Keep your text').click();
		flushSync();
		confirm('Keep your text');

		await vi.waitFor(() => expect(document.body.textContent).toContain('file, second version'));
		expect(document.body.textContent).toContain('This document changed again. Choose which text to keep.');
		expect(document.body.textContent).toContain('my typed text');
		expect(existingDraft(doc)).toMatchObject({ editText: 'my typed text', stale: true, status: 'error', error: EDITOR_STALE_STATUS });
		expect(onUseFile).not.toHaveBeenCalled();
	});
});
