import { afterEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { buildPaneConfig } from '$lib/homePaneConfig';
import { activeThreadId } from '$lib/stores/threads';
import Pane from './Pane.svelte';
import HomeShell from './HomeShell.svelte';

let component: Record<string, unknown> | undefined;
let host: HTMLElement | undefined;

afterEach(() => {
	if (component) unmount(component);
	host?.remove();
	activeThreadId.set(null);
});

it('offers the documents route on a project landing without a chat', () => {
	activeThreadId.set(null);
	host = document.createElement('div');
	document.body.appendChild(host);
	const onBrowseProjectDocuments = vi.fn();
	const config = buildPaneConfig('chat', null, { id: 'project', name: 'Project' })!;
	component = mount(Pane, { target: host, props: { ...config, onBrowseProjectDocuments } });
	flushSync();
	expect(host.textContent).not.toContain('No documents yet');
	const button = [...host.querySelectorAll('button')].find((item) => item.textContent?.includes('Browse documents…'));
	expect(button).toBeTruthy();
	button!.click();
	expect(onBrowseProjectDocuments).toHaveBeenCalledOnce();
});

it('routes the project browse control to the full documents library', () => {
	activeThreadId.set(null);
	host = document.createElement('div');
	document.body.appendChild(host);
	const onDocuments = vi.fn();
	const noop = vi.fn();
	component = mount(HomeShell, { target: host, props: {
		activeView: 'chat', sidebarOpen: false, conductorThreadId: null, systemStatus: {}, isDraggingOver: false,
		paneConfig: buildPaneConfig('chat', null, { id: 'project', name: 'Project' }), onDocuments,
		onNewChat: noop, onActivity: noop, onConductor: noop, onAttachProject: noop, onSelectThread: noop,
		onDeleteThread: noop, onArchiveThread: noop, onOpenSettings: noop, onToggleSidebar: noop,
		onTogglePane: noop, onOpenLightbox: noop, onAutoCreateThread: noop, onPickProject: noop,
		onPickProjectAndClosePane: noop, onCloseSettings: noop, onMeasurement: noop, onCloseMeasurement: noop,
		onGoWorkspace: noop,
	} });
	flushSync();
	const button = [...host.querySelectorAll('button')].find((item) => item.textContent?.includes('Browse documents…'));
	expect(button).toBeTruthy();
	button!.click();
	expect(onDocuments).toHaveBeenCalledExactlyOnceWith('all');
});
