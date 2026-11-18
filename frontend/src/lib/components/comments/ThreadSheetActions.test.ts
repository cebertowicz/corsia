import { afterEach, describe, expect, it, vi } from 'vitest';
import { mount, unmount } from 'svelte';
import type { DocComment } from '$lib/stores/docComments';
import ThreadSheetActions from './ThreadSheetActions.svelte';

let host: HTMLElement | null = null;
let component: Record<string, unknown> | null = null;
const base: DocComment = { id: 'c1', from: 0, to: 5, anchor: 'alpha', author: 'you', text: 'hello', at: 'now', resolved: false, replies: [] };

function render(head: DocComment) {
	host = document.createElement('div'); document.body.appendChild(host);
	component = mount(ThreadSheetActions, { target: host, props: { head, onAgree: vi.fn(), onResolve: vi.fn() } });
	return [...host.querySelectorAll('button')].map(button => button.textContent?.trim());
}

afterEach(() => { if (component) unmount(component); host?.remove(); component = null; host = null; });

describe('Agree and Resolve in the phone thread sheet header', () => {
	it('shows both on an open plain comment', () => {
		expect(render({ ...base, replacement: null })).toEqual(['Agree', 'Resolve']);
	});

	it('shows neither on a pending suggestion, a deletion (replacement "") included', () => {
		expect(render({ ...base, replacement: 'new text' })).toEqual([]);
		unmount(component!); host!.remove();
		expect(render({ ...base, replacement: '' })).toEqual([]);
	});

	it('shows only Resolve on a decided suggestion', () => {
		expect(render({ ...base, replacement: 'new text', decision: 'accepted' })).toEqual(['Resolve']);
	});
});
