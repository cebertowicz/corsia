import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { activeThreadId, threads } from '$lib/stores/threads';
import { canvasHeader } from '$lib/stores/canvasHeader';
import { uiScope } from '$lib/stores/uiScope';
import { resetSettingsNavigation, selectSettingsSection, settingsNavigation } from '$lib/stores/settingsNavigation';
import { threadSettingsOpen } from '$lib/stores/threadSettings';
import { measurementScope } from '$lib/stores/measurement';
import { projects } from '$lib/stores/projects';
import { measurementOrigin } from '$lib/stores/measurementNavigation';
import Header from './Header.svelte';
import headerSource from './Header.svelte?raw';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	canvasHeader.set(null);
	uiScope.set(null);
	resetSettingsNavigation();
	threadSettingsOpen.set(false);
	measurementScope.set({ project: 'all', agent: null });
	measurementOrigin.set(null);
	projects.set([]);
	threads.set([{
		id: 'thread-1', user_id: 'u', profile: 'personal', title: 'My thread',
		model: null, created_at: 'now', updated_at: 'now',
	}]);
	activeThreadId.set('thread-1');
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	activeThreadId.set(null);
	threads.set([]);
	measurementScope.set({ project: 'all', agent: null });
	measurementOrigin.set(null);
});

function render(activeView: 'activity' | 'chat' | 'measurement' = 'chat', conductorThreadId: string | null = null, onMeasurement = () => {}, hasPane = true): void {
	if (component) unmount(component);
	component = mount(Header, {
		target: host,
		props: { onToggleSidebar: () => {}, onTogglePane: () => {}, onMeasurement, activeView, conductorThreadId, hasPane },
	});
	flushSync();
}

describe('Header', () => {
	it('shows the panel toggle only where the view has a pane', () => {
		render('chat', null, () => {}, true);
		expect(host.querySelector('[aria-label="Toggle panel"]')).not.toBeNull();
		render('chat', null, () => {}, false);
		expect(host.querySelector('[aria-label="Toggle panel"]')).toBeNull();
	});

	it('keeps exactly one gear in a thread header', () => {
		render();
		expect(host.querySelectorAll('[aria-label$="settings"]')).toHaveLength(1);
		expect(host.querySelector('[aria-label="Thread settings"]')).not.toBeNull();
	});

	it('keeps an active thread chat-first without a Plan takeover action', () => {
		render();
		expect(host.textContent).toContain('My thread');
		expect(host.querySelector('[aria-label="Open this thread\'s plan"]')).toBeNull();
	});

	it('labels the no-session landing as the CORSIA agent', () => {
		activeThreadId.set(null);
		threads.set([]);
		render();
		expect(host.textContent).toContain('Workspace');
		expect(host.textContent).toContain('CORSIA');
		expect(host.textContent).not.toContain('conductor');
	});

	it('opens Settings on the scoped project\'s page from a project activity header', () => {
		activeThreadId.set(null);
		uiScope.set({ id: 'project-1', name: 'CORSIA', color: '#d65a87' });
		canvasHeader.set({ title: 'CORSIA' });
		render('activity');
		expect(host.querySelector('[aria-label="Thread settings"]')).toBeNull();
		const before = get(settingsNavigation).openRequest;
		(host.querySelector('[aria-label="Project settings"]') as HTMLButtonElement).click();
		expect(get(settingsNavigation).openRequest).toBe(before + 1);
		expect(get(settingsNavigation).section).toBe('projects');
		expect(get(settingsNavigation).project).toBe('project-1');
	});

	it('opens Settings at its first section from the workspace activity header', () => {
		activeThreadId.set(null);
		selectSettingsSection('debug');
		render('activity');
		const before = get(settingsNavigation).openRequest;
		(host.querySelector('[aria-label="Workspace settings"]') as HTMLButtonElement).click();
		expect(get(settingsNavigation).openRequest).toBe(before + 1);
		expect(get(settingsNavigation).section).toBe('providers');
		expect(get(settingsNavigation).project).toBeNull();
	});

	it('shows conductor settings for the designated hidden thread, not a project gear', () => {
		threads.set([]);
		activeThreadId.set('conductor-1');
		uiScope.set({ id: 'project-1', name: 'CORSIA', color: '#d65a87' });
		render('chat', 'conductor-1');
		expect(host.querySelector('[aria-label="Project settings"]')).toBeNull();
		(host.querySelector('[aria-label="Conductor settings"]') as HTMLButtonElement).click();
		let opened = false;
		threadSettingsOpen.subscribe((value) => { opened = value; })();
		expect(opened).toBe(true);
	});

	it('labels the settings gear CORSIA settings for the workspace scope\'s designated thread', () => {
		threads.set([]);
		activeThreadId.set('agent-1');
		uiScope.set(null);
		render('chat', 'agent-1');
		expect(host.querySelector('[aria-label="CORSIA settings"]')).not.toBeNull();
		expect(host.querySelector('[aria-label="Conductor settings"]')).toBeNull();
	});

	const agentThread = {
		id: 'thread-1', user_id: 'u', profile: 'personal', title: 'Bugs', model: null,
		created_at: 'now', updated_at: 'now', project_id: 'prj-atlas',
		agent_enabled: 1, agent_status: 'active', agent_responsibility: 'Bounded defects',
	};

	const measuresButton = () => [...host.querySelectorAll('button')]
		.find((button) => button.getAttribute('aria-label')?.startsWith('Measurement for')) as HTMLButtonElement | undefined;

	it('opens the Measures doorway for an agent thread scoped to that agent (PL-2)', () => {
		let opened = 0;
		threads.set([agentThread]);
		uiScope.set({ id: 'prj-atlas', name: 'Atlas', color: '#d65a87' });
		render('chat', null, () => { opened += 1; });

		measuresButton()?.click();
		flushSync();
		expect(opened).toBe(1);
		expect(get(measurementScope)).toEqual({ project: 'prj-atlas', agent: 'thread-1' });
		expect(get(measurementOrigin)).toEqual({ kind: 'thread', threadId: 'thread-1' });
	});

	it('is an icon with a named label, never text, and sits outside the overflow sheet (PL-2)', () => {
		threads.set([agentThread]);
		uiScope.set({ id: 'prj-atlas', name: 'Atlas', color: '#d65a87' });
		render('chat');
		const icon = measuresButton();
		expect(icon?.querySelector('svg')).not.toBeNull();
		expect(icon?.textContent?.trim()).toBe('');
		expect(icon?.getAttribute('aria-label')).toBe('Measurement for Bugs');
		expect(icon?.getAttribute('title')).toBe('Measurement for Bugs');
		expect(icon?.closest('.h-overflow-sheet')).toBeNull();
		expect(host.querySelector('[aria-label="More actions"]')).toBeNull();
	});

	it('opens a conductor thread doorway at its project, not at the conductor (PL-2)', () => {
		threads.set([]);
		activeThreadId.set('conductor-1');
		uiScope.set({ id: 'prj-atlas', name: 'Atlas', color: '#d65a87' });
		render('chat', 'conductor-1');

		measuresButton()?.click();
		flushSync();
		expect(get(measurementScope)).toEqual({ project: 'prj-atlas', agent: null });
		expect(get(measurementOrigin)).toEqual({ kind: 'thread', threadId: 'conductor-1' });
	});

	it('offers no Measures doorway for an ordinary conversation or outside chat', () => {
		render('chat');
		expect(measuresButton()).toBeUndefined();
		threads.set([agentThread]);
		render('activity');
		expect(measuresButton()).toBeUndefined();
	});
});

describe('Header title on the Measurement page', () => {
	const title = () => host.querySelector('.h-title')?.textContent?.replace(/\s+/g, ' ').trim();
	const project = { id: 'prj-atlas', name: 'Atlas', status: 'active', accent_color: null };
	const lane = (id: string, name: string) => ({
		id, user_id: 'u', profile: 'personal', title: name, model: null, created_at: 'now', updated_at: 'now',
		project_id: 'prj-atlas', agent_enabled: 1, agent_status: 'active', agent_responsibility: 'Bounded work',
	});

	beforeEach(() => {
		projects.set([project]);
		uiScope.set({ id: 'prj-atlas', name: 'Atlas', color: '#d65a87' });
		threads.set([lane('thread-1', 'Bugs'), lane('thread-2', 'Quality Gate')]);
	});

	it.each([['thread-1', 'Bugs'], ['thread-2', 'Quality Gate']])('names the project, not the %s thread the rail link was clicked from', (threadId, name) => {
		activeThreadId.set(threadId);
		measurementScope.set({ project: 'prj-atlas', agent: null });
		render('measurement');
		expect(title()).toBe('Atlas');
		expect(host.textContent).not.toContain(name);
	});

	it('names the project when the active thread is the conductor', () => {
		threads.set([]);
		activeThreadId.set('conductor-1');
		measurementScope.set({ project: 'prj-atlas', agent: null });
		render('measurement', 'conductor-1');
		expect(title()).toBe('Atlas');
		expect(host.textContent).not.toContain('conductor');
	});

	it('names the agent when the agent scope is open', () => {
		activeThreadId.set('thread-1');
		measurementScope.set({ project: 'prj-atlas', agent: 'thread-1' });
		render('measurement');
		expect(title()).toBe('Bugs');
	});

	it('follows the scope picker to another agent or to All work without waiting for a thread change', () => {
		activeThreadId.set('thread-1');
		measurementScope.set({ project: 'prj-atlas', agent: 'thread-2' });
		render('measurement');
		expect(title()).toBe('Quality Gate');
		measurementScope.set({ project: 'all', agent: null });
		flushSync();
		expect(title()).toBe('All work');
	});

	it('names the project from the open scope while the project list is still loading', () => {
		projects.set([]);
		activeThreadId.set('thread-1');
		measurementScope.set({ project: 'prj-atlas', agent: null });
		render('measurement');
		expect(title()).toBe('Atlas');
	});

	it('keeps the thread title in chat, where the title is the thread', () => {
		activeThreadId.set('thread-1');
		render('chat');
		expect(title()).toContain('Bugs');
	});
});

describe('Header glass layers', () => {
	const rule = (css: string, selector: string) => {
		const start = css.indexOf(`${selector} {`);
		return start < 0 ? '' : css.slice(start, css.indexOf('}', start));
	};
	const phone = headerSource.slice(headerSource.indexOf('@media (max-width: 768px)'));

	it('keeps the phone header blur on the masked layer so it fades out at the bottom', () => {
		// An unmasked blur layer ends in a hard line across the transcript.
		expect(rule(phone, '.shell-header::before')).not.toMatch(/backdrop-filter: none;/);
		const base = rule(headerSource, '.shell-header::before');
		expect(base).toMatch(/-webkit-backdrop-filter: var\(--glass-blur\);/);
		expect(base).toMatch(/mask-image: linear-gradient/);
	});

	it('keeps the phone glass opaque behind the header row', () => {
		// The transcript shows through wherever the mask is below 1, so it stays opaque down to
		// the bottom of the row's glyphs, 9px below the row centre, and only then fades.
		expect(rule(phone, '.shell-header')).toContain('padding-top: var(--header-pad-top);');
		expect(rule(phone, '.shell-header::before')).toContain(
			'mask-image: linear-gradient(180deg, rgba(0,0,0,1) calc(50% + var(--header-pad-top) / 2 + 9px), transparent 100%);',
		);
	});
});
