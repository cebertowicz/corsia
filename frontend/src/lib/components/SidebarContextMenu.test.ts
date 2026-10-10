import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, unmount } from 'svelte';
import SidebarContextMenu from './SidebarContextMenu.svelte';

let host: HTMLElement;
let component: Record<string, unknown> | null;

const actions = {
	onToggleUnread: vi.fn(),
	onPin: vi.fn(),
	onRename: vi.fn(),
	onAgentSettings: vi.fn(),
	onMoveToProject: vi.fn(),
	onArchive: vi.fn(),
	onDeactivateAgent: vi.fn(),
	onDelete: vi.fn(),
	onClose: vi.fn(),
};

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	component = null;
	vi.clearAllMocks();
});

afterEach(() => {
	if (component) unmount(component);
	host.remove();
});

function render(kind: 'conversation' | 'agent' | 'conductor', isUnread = false, canMarkSeen = true) {
	component = mount(SidebarContextMenu, {
		target: host,
		props: {
			threadId: `${kind}-1`,
			kind,
			isUnread,
			canMarkSeen,
			x: 20,
			y: 20,
			...actions,
		},
	});
}

function labels(): string[] {
	return Array.from(host.querySelectorAll('.context-menu > button')).map((button) =>
		button.textContent?.trim() ?? '',
	);
}

describe('SidebarContextMenu variants', () => {
	it('offers durable Agent actions without conversation-only destructive actions', () => {
		render('agent');

		expect(labels()).toEqual([
			'Mark unread',
			'Rename',
			'Agent settings',
			'Deactivate Agent',
		]);
	});

	it('offers only the valid unread action for the Conductor', () => {
		render('conductor', true);

		expect(labels()).toEqual(['Mark read']);
	});

	it('adds unread control to the existing conversation actions', () => {
		render('conversation');

		expect(labels()).toContain('Mark unread');
		expect(labels()).toContain('Pin to top');
		expect(labels()).toContain('Archive');
		expect(labels()).toContain('Delete');
	});

	it('hides Mark read/unread for a thread the user has never opened', () => {
		render('agent', false, false);

		expect(labels()).toEqual(['Rename', 'Agent settings', 'Deactivate Agent']);
	});
});
