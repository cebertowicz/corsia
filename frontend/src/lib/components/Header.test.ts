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
import { activityPhoneItem } from '$lib/stores/activityPhoneItem';
import { syncCanvasHeader } from '$lib/homeCanvasHeader';
import Header from './Header.svelte';
import headerSource from './Header.svelte?raw';
import appHtml from '../../app.html?raw';

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

function render(
	activeView: 'activity' | 'chat' | 'measurement' = 'chat',
	conductorThreadId: string | null = null,
	onMeasurement = () => {},
	hasPane = true,
	artifactHeaderInset: number | null = null,
	onTogglePane = () => {},
): void {
	if (component) unmount(component);
	component = mount(Header, {
		target: host,
		props: { onToggleSidebar: () => {}, onTogglePane, onMeasurement, activeView, conductorThreadId, hasPane, artifactHeaderInset },
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
		expect(host.querySelector('[aria-label="Chat settings"]')).not.toBeNull();
	});

	it('keeps the canvas-open desktop controls in the header', () => {
		let measuresOpened = 0;
		let panelToggles = 0;
		threads.set([{ ...agentThread }]);
		render('chat', null, () => { measuresOpened += 1; }, true, 600, () => { panelToggles += 1; });

		const right = host.querySelector('.h-right');
		expect(right).not.toBeNull();
		const measurement = right?.querySelector('[aria-label^="Measurement for"]') as HTMLButtonElement;
		const settings = right?.querySelector('[aria-label="Chat settings"]') as HTMLButtonElement;
		const panel = right?.querySelector('[aria-label="Toggle panel"]') as HTMLButtonElement;
		expect(measurement).toBeTruthy();
		expect(settings).toBeTruthy();
		expect(panel).toBeTruthy();

		measurement.click();
		settings.click();
		panel.click();
		expect(measuresOpened).toBe(1);
		expect(get(threadSettingsOpen)).toBe(true);
		expect(panelToggles).toBe(1);
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
		expect(host.querySelector('[aria-label="Chat settings"]')).toBeNull();
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

describe('Header title overflow', () => {
	it('lets a long scope title shrink and ellipsize before it displaces fixed controls', () => {
		// The base rule (a document's name is drawn by HeaderDocTitle, which cuts it in the middle).
		const start = headerSource.indexOf('\t.h-scope {');
		const rule = headerSource.slice(start, headerSource.indexOf('}', start));
		expect(rule).toContain('min-width: 0;');
		expect(rule).toContain('max-width: 24rem;');
		expect(rule).toContain('text-overflow: ellipsis;');
	});
});

describe('Header for an open Activity item on a phone', () => {
	const rule = (css: string, selector: string) => {
		const start = css.indexOf(`${selector} {`);
		return start < 0 ? '' : css.slice(start, css.indexOf('}', start));
	};
	const phone = headerSource.slice(headerSource.indexOf('@media (max-width: 768px)'));

	function openItem() {
		activityPhoneItem.set({ backLabel: 'Notifications', onBack: () => {} });
		syncCanvasHeader({
			threadSettingsOpen: false, compactArtifact: null, contextAttachTarget: null, activeView: 'activity',
			uiScopeName: null, activeThreadId: null, conductorThreadId: null, threads: [],
			closeThreadSettings: () => {}, closeSettings: () => {}, closeDocuments: () => {},
		});
		render('activity');
	}

	afterEach(() => activityPhoneItem.set(null));

	it('reads ‹ Notifications, with its label kept at phone width', () => {
		openItem();
		const header = host.querySelector('.shell-header')!;
		expect(header.classList.contains('links')).toBe(true);
		expect(host.querySelector('.h-back .h-back-label')?.textContent).toBe('Notifications');
		// The phone rule hides every Back label; the Activity item's header brings its label back.
		expect(rule(phone, '.h-back-label')).toContain('display: none;');
		expect(rule(phone, '.links .h-back-label')).toContain('display: inline;');
	});

	it('draws Back in the link colour, with no Next beside it', () => {
		openItem();
		expect(host.querySelector('.shell-header')!.classList.contains('links')).toBe(true);
		expect(host.querySelector('.h-act')).toBeNull();
		expect(rule(headerSource, '.links .h-back, .links .h-act:not(:disabled)')).toContain('color: var(--accent-text);');
	});

	it('leaves other back headers as they were', () => {
		canvasHeader.set({ title: 'Settings', backLabel: 'Chat settings', onBack: () => {} });
		render('chat');
		expect(host.querySelector('.shell-header')!.classList.contains('links')).toBe(false);
	});
});

describe('Header glass layers', () => {
	const rule = (css: string, selector: string) => {
		const start = css.indexOf(`${selector} {`);
		return start < 0 ? '' : css.slice(start, css.indexOf('}', start));
	};
	const phone = headerSource.slice(headerSource.indexOf('@media (max-width: 768px)'));

	it('keeps the full blur, in both spellings, on the phone header layer', () => {
		const phoneLayer = rule(phone, '.shell-header::before');
		expect(phoneLayer).not.toMatch(/backdrop-filter: none;/);
		const base = rule(headerSource, '.shell-header::before');
		expect(base).toMatch(/\n\t\tbackdrop-filter: var\(--glass-blur\);/);
		expect(base).toMatch(/-webkit-backdrop-filter: var\(--glass-blur\);/);
	});

	it('lets the installed iOS app draw the page under a translucent status bar', () => {
		// With `default` iOS draws an opaque bar above the page: white in light mode, the page's own dark colour in dark mode.
		expect(appHtml).toContain('<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />');
	});

	it('fades the phone header out over two stacked blur layers that run below the bar', () => {
		// WebKit masks the blurred backdrop too, so one masked layer shows the sharp transcript
		// where it fades. Both layers end --h-fade (76px) below the row centre, past the bar, so
		// text scrolling under the bar fades into the frost with no edge. The full blur and tint
		// step down through eased stops; a light blur on top runs to the same end.
		expect(rule(phone, '.shell-header')).toContain('--h-fade: 76px;');
		const full = rule(phone, '.shell-header::before');
		const reach = 'inset: 0 0 calc(var(--header-bottom) - var(--h-row-centre) - var(--h-fade)) 0;';
		expect(full).toContain(reach);
		expect(full).toContain('color-mix(in srgb, var(--header-top-base) 72%, transparent) calc(var(--h-row-centre) + 6px)');
		expect(full).toContain('color-mix(in srgb, var(--header-top-base) 12%, transparent) calc(var(--h-row-centre) + 58px)');
		expect(full).toContain('transparent calc(var(--h-row-centre) + var(--h-fade))');
		expect(full).toContain('-webkit-mask-image: var(--h-mask); mask-image: var(--h-mask);');
		expect(full).toContain('--h-mask: linear-gradient(');
		expect(full).toContain('color-mix(in srgb, currentColor 62%, transparent) calc(var(--h-row-centre) + 34px)');
		const soft = rule(phone, '.shell-header::after');
		expect(soft).toContain(reach);
		expect(soft).toContain('backdrop-filter: var(--glass-blur-soft);');
		expect(soft).toContain('-webkit-backdrop-filter: var(--glass-blur-soft);');
		expect(soft).toContain('--h-mask: linear-gradient(180deg, currentColor calc(var(--h-row-centre) + 20px), transparent calc(var(--h-row-centre) + var(--h-fade)));');
		expect(soft).toContain('-webkit-mask-image: var(--h-mask); mask-image: var(--h-mask);');
		expect(soft).toContain('pointer-events: none;');
	});

	it('sizes the phone header and centres the row at the shared top-row centre', () => {
		// The iOS 27 system blur covers the top of the page to about safe-top + 34px; an 18px row
		// centred at --top-row-centre (safe-top + 36px under an inset) is sharp. In a tab the
		// centre stays 36px. The padding and both fade stops come from the one centre.
		const phoneHeader = rule(phone, '.shell-header');
		expect(phoneHeader).toContain('height: var(--header-bottom);');
		expect(phoneHeader).toContain('--h-row-centre: max(var(--top-row-clearance), var(--top-row-centre));');
		expect(phoneHeader).toContain('padding-top: calc(2 * var(--h-row-centre) - var(--header-bottom));');
	});

	it('makes the phone header solid when transparency is reduced', () => {
		// Reduced transparency turns --glass-blur off, so a part-transparent tint would show the
		// transcript sharp through the bar.
		const at = headerSource.indexOf('@media (max-width: 768px) and (prefers-reduced-transparency: reduce)');
		expect(at).toBeGreaterThan(-1);
		const solid = rule(headerSource.slice(at), '.shell-header::before');
		expect(solid).toContain('background-color: var(--header-top-base);');
		// The solid bar ends at the bar's bottom; the fade below it exists only with the blur.
		expect(solid).toContain('inset: 0;');
		expect(solid).toContain('background-image: none;');
		expect(solid).toContain('-webkit-mask-image: none;');
	});
});
