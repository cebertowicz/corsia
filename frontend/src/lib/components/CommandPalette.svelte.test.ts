import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import CommandPalette from './CommandPalette.svelte';
import { paletteOpen } from '$lib/stores/palette';
import { threads } from '$lib/stores/threads';
import { _setProjectsForTests } from '$lib/stores/projects';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
let onPickProject: ReturnType<typeof vi.fn>;
let onPickConversation: ReturnType<typeof vi.fn>;

function renderPalette() {
	component = mount(CommandPalette, {
		target: host,
		props: {
			onPickProject,
			onPickConversation,
			onNewChat: vi.fn(),
			onGoWorkspace: vi.fn(),
		},
	});
	paletteOpen.set(true);
	flushSync();
}

function typeQuery(value: string) {
	const input = host.querySelector('input')!;
	input.value = value;
	input.dispatchEvent(new Event('input', { bubbles: true }));
	flushSync();
}

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	_setProjectsForTests([
		{ id: 'project-alpha', name: 'Alpha', status: 'active', accent_color: null },
		{ id: 'project-beta', name: 'Beta', status: 'active', accent_color: null },
	]);
	threads.set([
		{
			id: 'conversation-beta', user_id: 'user-1', profile: 'personal', title: 'Beta conversation',
			model: 'test', created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-01T00:00:00Z',
		},
	]);
	onPickProject = vi.fn();
	onPickConversation = vi.fn();
	Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() });
});

afterEach(() => {
	paletteOpen.set(false);
	threads.set([]);
	_setProjectsForTests([]);
	if (component) unmount(component);
	component = null;
	host.remove();
	delete (HTMLElement.prototype as { scrollIntoView?: unknown }).scrollIntoView;
});

describe('CommandPalette query results', () => {
	it('hides unmatched actions while showing empty result groups', () => {
		renderPalette();
		typeQuery('no-match');

		expect(host.textContent).not.toContain('New chat');
		expect(host.textContent).not.toContain('Go to workspace');
		expect(host.textContent).not.toContain('New project');
		expect(host.textContent).toContain('No matching projects.');
		expect(host.textContent).toContain('No matching conversations.');
	});

	it('highlights the first visible match and Enter invokes that match', () => {
		renderPalette();
		typeQuery('alpha');

		expect(host.querySelector('.res.sel')?.textContent).toContain('Alpha');
		window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));

		expect(onPickProject).toHaveBeenCalledWith(expect.objectContaining({ id: 'project-alpha' }));
		expect(onPickConversation).not.toHaveBeenCalled();
	});

});
