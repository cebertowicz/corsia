import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import SidebarAttentionList from './SidebarAttentionList.svelte';
import { threadPlanStates } from '$lib/stores/threadPlanStates';
import type { ThreadAttentionItem } from '$lib/api/plans';
import * as productEvents from '$lib/product_events';
import { registerNodeFocus, registerPlanSurface } from '$lib/planNodeFocus';
import { activeThreadId } from '$lib/stores/threads';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
const access: ThreadAttentionItem = { kind: 'access', title: 'Read diagnostics', node_id: null };

function render(onSelect = vi.fn(), items: ThreadAttentionItem[] = [access, access]) {
	threadPlanStates.set({
		'thread-1': { state: 'blocked_on_you', label_key: null, paused: false, attention_items: items },
	});
	component = mount(SidebarAttentionList, {
		target: host,
		props: { attention: { threadId: 'thread-1', title: '2 for you' }, onSelect },
	});
	flushSync();
	return onSelect;
}

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	activeThreadId.set('thread-1');
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	threadPlanStates.set({});
});

describe('SidebarAttentionList', () => {
	it('lists two access requests with the same purpose', () => {
		render();
		expect(document.querySelectorAll('.attention-item')).toHaveLength(2);
	});

	it('shows the mark\'s own word for each item kind, not the raw kind', () => {
		render();
		const words = [...document.querySelectorAll('.attention-item small')].map((el) => el.textContent);
		expect(words).toEqual(['Allow access', 'Allow access']);
	});

	it('hands the selected node to its thread’s plan pane once navigation completes', async () => {
		let completeNavigation!: () => void;
		const focus = vi.fn();
		const unregister = [registerPlanSurface(() => true), registerNodeFocus('thread-1', focus)];
		const onSelect = render(
			vi.fn(() => new Promise<void>((resolve) => { completeNavigation = resolve; })),
			[{ kind: 'decision', title: 'Choose a model', node_id: 'node-1' }],
		);
		(document.querySelector('.attention-item') as HTMLButtonElement).click();
		await vi.waitFor(() => expect(onSelect).toHaveBeenCalledWith('thread-1'));
		expect(focus).not.toHaveBeenCalled();

		completeNavigation();
		await vi.waitFor(() => expect(focus).toHaveBeenCalledWith('node-1', null));
		expect(focus).toHaveBeenCalledTimes(1);
		for (const off of unregister) off();
	});

	it('emits a product event when an item is selected', () => {
		const spy = vi.spyOn(productEvents, 'emitProductEvent');
		render();
		(document.querySelector('.attention-item') as HTMLButtonElement).click();
		expect(spy).toHaveBeenCalledWith('sidebar', 'attention_item_selected', expect.objectContaining({ thread_id: 'thread-1', kind: 'access' }));
	});

	it('opens the thread from an item and closes without opening it', () => {
		const onSelect = render();
		(document.querySelector('.attention-item') as HTMLButtonElement).click();
		flushSync();
		expect(onSelect).toHaveBeenCalledWith('thread-1');

		unmount(component!);
		onSelect.mockClear();
		render(onSelect);
		(document.querySelector('[aria-label="Close"]') as HTMLButtonElement).click();
		flushSync();
		expect(onSelect).not.toHaveBeenCalled();
	});
});
