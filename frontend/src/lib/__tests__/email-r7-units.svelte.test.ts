import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it } from 'vitest';
import ExternalReviewRow from '$lib/components/attention/ExternalReviewRow.svelte';
import { docOpenFailure, ImageFileError, PdfFileError } from '$lib/docOpenRefusal';
import type { ExternalReviewDetail } from '$lib/api/attentionQueue';
import { emptyWorkspace, openTab } from '$lib/artifactTabs';

let component: Record<string, unknown> | null = null;
afterEach(() => { if (component) unmount(component); component = null; document.body.innerHTML = ''; });

const review = (requester: ExternalReviewDetail['requester']): ExternalReviewDetail => ({
	review_key: 'k', title: 'Review the reply', requester, pending_count: 1, total_count: 1, node_id: 'n',
	destination: { pane: 'documents', id: 'a1', path: 'email-0123456789ab.md', title: 'email-0123456789ab.md' }, source: null,
});
const eyebrow = (requester: ExternalReviewDetail['requester']) => {
	component = mount(ExternalReviewRow, { target: document.body, props: { review: review(requester) } });
	flushSync();
	return document.querySelector('.eyebrow')!.textContent;
};

describe('the review dock’s eyebrow', () => {
	it('names an agent as "Review · Name (agent)" with its spaces', () => {
		expect(eyebrow({ kind: 'thread', label: 'Email drafter', lane: 'x' })).toBe('Review · Email drafter (agent)');
	});
	it('names a person without a kind', () => {
		expect(eyebrow({ kind: 'user', label: 'Steve', lane: 'x' })).toBe('Review · Steve');
	});
});

describe('a text open’s typed refusal', () => {
	it('tells a PDF from an image', () => {
		expect(docOpenFailure({ code: 'pdf_file' })).toBeInstanceOf(PdfFileError);
		expect(docOpenFailure({ code: 'image_file' })).toBeInstanceOf(ImageFileError);
	});
});

describe('the review dock’s open button', () => {
	const label = (destination: ExternalReviewDetail['destination'], purpose?: 'send') => {
		component = mount(ExternalReviewRow, { target: document.body, props: { review: { ...review(null), destination, purpose } } });
		flushSync();
		return document.querySelector('.actions button:last-child')!.textContent?.trim();
	};
	it('says Open draft only for a review its producer marked as the send review', () => {
		expect(label({ pane: 'documents', id: 'a1', path: 'work/drafts/email-0123456789ab.md' }, 'send')).toBe('Open draft');
	});
	it('says Open document for any other review with a path, whatever the file is called', () => {
		expect(label({ pane: 'documents', id: 'a1', path: 'work/drafts/email-0123456789ab.md' })).toBe('Open document');
		expect(label({ pane: 'documents', id: 'a1', path: 'notes/plan.md' })).toBe('Open document');
	});
	it('keeps naming the pane when there is no path', () => {
		expect(label({ pane: 'documents', id: 'a1' }, 'send')).toBe('Open in documents');
	});
});

describe('a file opened from a draft’s chip', () => {
	it('remembers the draft tab it came from, and a reopen from anywhere else forgets it', () => {
		const draft = openTab(emptyWorkspace(), { path: 'email-0123456789ab.md', title: 'Draft' });
		const draftId = draft.activeTabId!;
		const fromChip = openTab(draft, { path: 'waiver.pdf', title: 'waiver.pdf', openedFrom: draftId });
		expect(fromChip.tabs.find((t) => t.path === 'waiver.pdf')!.openedFrom).toBe(draftId);
		const reopened = openTab(fromChip, { path: 'waiver.pdf', title: 'waiver.pdf' });
		expect(reopened.tabs.find((t) => t.path === 'waiver.pdf')!.openedFrom).toBeUndefined();
	});
});
