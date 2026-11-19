import { flushSync, mount, unmount } from 'svelte';
import { expect, it, vi } from 'vitest';
import { authorsWithOpenSuggestions } from '$lib/docCommentDrawer';
import type { DocComment } from '$lib/stores/docComments';
import SuggestionStatus from './SuggestionStatus.svelte';

const suggestion = (id: string, author: string, extra: Partial<DocComment> = {}) => ({ id, author, replacement: 'new text', ...extra }) as DocComment;

it('offers bulk acceptance for two eligible suggestions across any authors', () => {
	const a = suggestion('a', 'Alex'), b = suggestion('b', 'Alex'), c = suggestion('c', 'Bo');
	expect(authorsWithOpenSuggestions([a])).toEqual([]);
	expect(authorsWithOpenSuggestions([a, b]).map(([name, rows]) => [name, rows.length])).toEqual([['Alex', 2]]);
	expect(authorsWithOpenSuggestions([a, c]).map(([name, rows]) => [name, rows.length])).toEqual([['Alex', 1], ['Bo', 1]]);
	expect(authorsWithOpenSuggestions([a, suggestion('d', 'Bo', { orphaned: true })])).toEqual([]);
});

it('accepts only checked authors through their bulk paths', async () => {
	const host = document.createElement('div'); document.body.append(host);
	const acceptAll = vi.fn().mockResolvedValue(true);
	const component = mount(SuggestionStatus, { target: host, props: {
		authors: [['Alex', [suggestion('a', 'Alex')]], ['Bo', [suggestion('b', 'Bo')]]],
		busy: null, shownBusy: null, skipped: [], error: '', acceptAll,
	} });
	try {
		flushSync(() => host.querySelector('button')!.click());
		const checks = host.querySelectorAll<HTMLInputElement>('input[type=checkbox]');
		expect([...checks].map((input) => input.checked)).toEqual([true, true]);
		flushSync(() => checks[1].click());
		const confirm = [...host.querySelectorAll('button')].find((button) => button.textContent === 'Accept selected')!;
		flushSync(() => confirm.click());
		await vi.waitFor(() => expect(acceptAll).toHaveBeenCalledWith('Alex', 'a'));
		expect(acceptAll).toHaveBeenCalledTimes(1);
		await vi.waitFor(() => expect(host.querySelectorAll('input')).toHaveLength(0));
		flushSync(() => host.querySelector('button')!.click());
		flushSync(() => [...host.querySelectorAll('button')].find((button) => button.textContent === 'Accept selected')!.click());
		await vi.waitFor(() => expect(acceptAll).toHaveBeenCalledTimes(3));
		expect(acceptAll.mock.calls.slice(1)).toEqual([['Alex', 'a'], ['Bo', 'b']]);
	} finally { await unmount(component); host.remove(); }
});
