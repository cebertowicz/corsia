/**
 * The chat slot owner, the suppression lifecycle, and identity teardown.
 *
 * Red at `2706b37a`: chat is the only surface in the codebase that puts a
 * control in a canvas bottom **without owning a slot for it**, so the dock sits
 * in `.chat-content`'s normal flow underneath the absolutely-positioned
 * composer; suppression lives in a module value that outlives every mount; and
 * the queue survives a sign-out.
 */
import { flushSync, mount, tick, unmount, type ComponentProps } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import HomeShell from '$lib/components/HomeShell.svelte';
import ChatAttentionStack from '$lib/components/attention/ChatAttentionStack.svelte';
import chatAttentionStackSource from '$lib/components/attention/ChatAttentionStack.svelte?raw';
import attentionDockSource from '$lib/components/attention/AttentionDock.svelte?raw';
import { token } from '$lib/stores/auth';
import { activeThreadId, threads } from '$lib/stores/threads';
import { __reset, expandedId, queue } from '$lib/stores/attentionDock.svelte';
import type { AttentionQueue } from '$lib/api/attentionQueue';
import type { HostAccessLease } from '$lib/api/hostAccess';
import { decisionItem, queueBody } from './fixtures/attentionQueue';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
let body: AttentionQueue;
let leases: HostAccessLease[];
let queueDelay: (() => Promise<void>) | null;

const EMPTY_RESPONSE = {
	contract_version: 2,
	as_of: 'now',
	target: { kind: 'workspace', id: 'ws', label: 'Workspace' },
	revision_token: '"r1"',
	items: [],
	attention: { awaiting_count: 0 },
};

function lease(state: HostAccessLease['state']): HostAccessLease {
	return {
		id: `lease-${state}`, root_thread_id: 'thread-1', root_thread_title: 'Parent work',
		state, purpose: 'Read one file outside the workspace',
		plan: ['stat the path'], requested_duration_seconds: 120,
		expected_resources: null, persistent_effects: null, rollback_plan: null,
		forbidden_effects: null, requesting_project_id: 'project-1',
		repo_policy_snapshot: [], repo_policy_hash: 'h', requested_policy_exceptions: [],
		application_hash: 'a', revision: 1, reviewer_model: null, reviewer: null,
		application_review_result: null, expires_at: null, terminal_reason: null,
		created_at: 't', updated_at: 't',
	} as HostAccessLease;
}

function stubNetwork() {
	vi.stubGlobal('fetch', vi.fn(async (url: string) => {
		const target = String(url);
		if (target.includes('/api/attention/queue') && queueDelay) await queueDelay();
		const payload = target.includes('/api/attention/queue')
			? body
			: target.includes('/api/host-access/leases')
				? { leases }
				: {};
		return {
			ok: true, status: 200,
			json: async () => payload,
			text: async () => JSON.stringify(payload),
			headers: { get: () => null },
		};
	}));
}

async function settle() {
	for (let i = 0; i < 12; i += 1) {
		await Promise.resolve();
		await tick();
		flushSync();
	}
}

const noop = () => {};
type ShellProps = ComponentProps<typeof HomeShell>;

function shellProps(activeView: string): ShellProps {
	return {
		activeView, sidebarOpen: false, chatPanelRef: undefined,
		conductorThreadId: null, systemStatus: null, isDraggingOver: false,
		paneConfig: { label: 'Workspace', sections: [] },
		onGoWorkspace: noop,
		onNewChat: noop, onConductor: noop, onDocuments: noop, onAttachProject: noop,
		onSelectThread: noop, onDeleteThread: noop, onArchiveThread: noop,
		onOpenSettings: noop, onToggleSidebar: noop, onTogglePane: noop,
		onOpenLightbox: noop, onAutoCreateThread: noop,
		onPickProject: noop, onPickProjectAndClosePane: noop,
		onCloseSettings: noop,
	} as unknown as ShellProps;
}

function stack(): HTMLElement | null {
	return host.querySelector('.chat-attention-stack');
}

function dockBar(): HTMLButtonElement | null {
	return host.querySelector('[data-attention-dock] .dock-bar');
}

function polyfillAnimate() {
	if (typeof Element.prototype.animate === 'function') return;
	Element.prototype.animate = function animate() {
		return {
			cancel() {}, finish() {}, play() {}, pause() {}, reverse() {},
			addEventListener() {}, removeEventListener() {},
			currentTime: 0, playState: 'finished', finished: Promise.resolve(),
			effect: { getComputedTiming: () => ({ duration: 0 }) },
		} as unknown as Animation;
	} as typeof Element.prototype.animate;
}

beforeEach(() => {
	polyfillAnimate();
	// The drawer portals to document.body, so the host is the body itself.
	host = document.body;
	token.set('test-bearer');
	body = queueBody(decisionItem(), 2);
	leases = [];
	queueDelay = null;
	__reset();
	vi.unstubAllGlobals();
	stubNetwork();
	threads.set([{ id: 'thread-1', title: 'Parent work', project_id: 'project-1' } as never]);
	activeThreadId.set('thread-1');
});
afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.replaceChildren();
	__reset();
	token.set('test-bearer');
	vi.restoreAllMocks();
});

describe('Chat slot owner — one component owns the bottom band', () => {
	it('is the only chat bottom-stack owner, and both controls sit inside it', async () => {
		leases = [lease('awaiting_user')];
		component = mount(HomeShell, { target: host, props: shellProps('chat') });
		await settle();

		expect(host.querySelectorAll('.chat-attention-stack').length).toBe(1);
		expect(stack()!.querySelector('[data-attention-dock]')).toBeTruthy();
		expect(stack()!.querySelector('.thread-host-access')).toBeTruthy();
	});

	it('lays the lease control out in normal flow rather than floating it', async () => {
		leases = [lease('awaiting_user')];
		component = mount(HomeShell, { target: host, props: shellProps('chat') });
		await settle();

		// H-B is unreachable precisely because an absolute control contributes
		// zero measurable height; the stack removes that class of bug.
		expect(stack()!.querySelector('.thread-host-access')!.classList)
			.toContain('stacked');
	});

	it('keeps the mobile attention band above the keyboard-lifted composer', () => {
		expect(chatAttentionStackSource).toContain('--chat-attention-keyboard-inset: 0px;');
		expect(chatAttentionStackSource).toMatch(
			/@media \(max-width: 768px\)[\s\S]*?--chat-attention-keyboard-inset:\s*var\(--kb-toolbar-inset, 0px\)/,
		);
		expect(chatAttentionStackSource).toMatch(
			/--chat-attention-available-height:\s*calc\([\s\S]*?- var\(--chat-attention-keyboard-inset\)/,
		);
		expect(chatAttentionStackSource).toMatch(
			/bottom:\s*calc\([\s\S]*?\+\s*var\(--chat-attention-keyboard-inset\)[\s\S]*?\+\s*8px\s*\)/,
		);
	});

	it('renders no stack children at all when nothing needs the user', async () => {
		body = queueBody(null, 0);
		component = mount(HomeShell, { target: host, props: shellProps('chat') });
		await settle();

		expect(stack()).toBeTruthy();
		expect(stack()!.children.length).toBe(0);
	});

	/**
	 * The lease control's classes for one lease state, mounted in isolation.
	 * Returns a plain snapshot taken while mounted, so no assertion depends on
	 * what a live DOMTokenList does after its component is torn down.
	 */
	async function leaseControlClasses(state: HostAccessLease['state']): Promise<string[]> {
		leases = [lease(state)];
		component = mount(HomeShell, { target: host, props: shellProps('chat') });
		await settle();
		const classes = [...stack()!.querySelector('.thread-host-access')!.classList];
		unmount(component);
		component = null;
		return classes;
	}

	it('gives the lease control the full band whenever it carries prose or controls', async () => {
		// The mobile rail's compact slot is a single ~1fr track. Measured at
		// 393px without `wide`, a transitional status wrapped its lease purpose
		// into a 94px column inside a 196x480px surface that ate the transcript.
		for (const state of ['reviewing', 'activating', 'awaiting_user'] as const) {
			expect(await leaseControlClasses(state), state).toContain('wide');
		}
	});

	it('leaves the bare active-lease chip in the compact rail slot', async () => {
		// An active lease renders only a shield-and-countdown chip, which is what
		// the three-slot rail was sized for; widening it would cost a whole row.
		expect(await leaseControlClasses('active')).not.toContain('wide');
	});

	it('styles that full-band occupant, so the rule is not dead CSS', () => {
		// `.wide` went unapplied by any component, so this rule silently matched
		// nothing and every host-access surface stayed in the narrow track.
		// `[^{}]*` keeps the declaration inside this rule's own braces — a lazy
		// `[\s\S]*?` would happily match a `grid-column` from any later rule.
		const wideRule = /\.thread-host-access\.wide\)\s*\{[^{}]*grid-column:\s*1 \/ -1[^{}]*\}/;
		expect(chatAttentionStackSource).toMatch(wideRule);

		// ...and that rule has to live inside the mobile media query, since the
		// narrow track it overrides only exists there.
		const mobileAt = chatAttentionStackSource.indexOf('@media (max-width: 768px)');
		expect(mobileAt).toBeGreaterThan(-1);
		expect(chatAttentionStackSource.slice(mobileAt)).toMatch(wideRule);
	});

	it('backs every line of dock copy that floats over the transcript', () => {
		// The dock bar stays frosted, but it starts after its container's solid
		// transcript-colour inset. That backing blocks message text before the bar's
		// translucent material composites over it. .dock-deferred is the
		// host-access-awaiting note this stack widens, and .dock-stale sits beside it.
		const ruleOf = (selector: string) => attentionDockSource.match(new RegExp(`\\${selector}[^{}]*\\{[^{}]*\\}`))?.[0] ?? '';
		const dock = ruleOf('.attention-dock');
		expect(dock, '.attention-dock should declare a rule').not.toBe('');
		expect(dock, '.attention-dock reserves its solid backing inset').toContain('padding: var(--space-4) var(--dock-gutter) var(--dock-gutter)');
		expect(dock, '.attention-dock paints the solid backing beneath the bar').toContain(
			'var(--bg-primary) var(--space-4)'
		);
		expect(ruleOf('.dock-bar'), '.dock-bar stays frosted over that backing').toContain('background: var(--glass-thin)');
		for (const selector of ['.dock-deferred', '.dock-stale']) {
			const body = ruleOf(selector);
			expect(body, `${selector} should declare a rule`).not.toBe('');
			expect(body, `${selector} needs opaque backing`).toContain('--glass-regular-thick');
			expect(body, `${selector} needs the blur its peers use`).toContain('--glass-blur');
		}
	});
});

describe('Suppression lifecycle — the flag cannot outlive its owner', () => {
	it('defers the dock while a lease awaits the user, keeping it focusable', async () => {
		leases = [lease('awaiting_user')];
		component = mount(HomeShell, { target: host, props: shellProps('chat') });
		await settle();

		const bar = dockBar()!;
		expect(bar.getAttribute('aria-disabled')).toBe('true');
		// `disabled` removes it from the tab order and says nothing; deferred is
		// not the same as absent.
		expect(bar.hasAttribute('disabled')).toBe(false);
		const described = bar.getAttribute('aria-describedby');
		expect(described).toBeTruthy();
		expect(document.getElementById(described!)?.textContent ?? '')
			.toMatch(/host-access/i);
	});

	it('releases suppression when the lease control unmounts with its session', async () => {
		leases = [lease('awaiting_user')];
		component = mount(HomeShell, { target: host, props: shellProps('chat') });
		await settle();
		expect(dockBar()!.getAttribute('aria-disabled')).toBe('true');

		// The session goes away, so the control leaves the DOM. A module-scoped
		// flag would stay set and strand the dock disabled with no explanation.
		activeThreadId.set(null);
		threads.set([]);
		await settle();

		expect(host.querySelector('.thread-host-access')).toBeNull();
		expect(dockBar()!.getAttribute('aria-disabled')).toBe('false');
	});

	it('a suppressed click is inert and never springs open afterwards', async () => {
		leases = [lease('awaiting_user')];
		component = mount(HomeShell, { target: host, props: shellProps('chat') });
		await settle();
		dockBar()!.click();
		await settle();
		expect(host.querySelectorAll('.decision-chip').length).toBe(0);

		leases = [];
		activeThreadId.set(null);
		threads.set([]);
		await settle();

		expect(host.querySelectorAll('.decision-chip').length).toBe(0);
	});

	it.each(['reviewing', 'activating', 'active'] as const)(
		'does not defer the dock for a %s lease — only one awaiting the user',
		async (state) => {
			leases = [lease(state)];
			component = mount(HomeShell, { target: host, props: shellProps('chat') });
			await settle();

			expect(dockBar()!.getAttribute('aria-disabled')).toBe('false');
		},
	);
});

describe('Identity teardown — sign-out takes the queue with it', () => {
	it('clears the bar, the expanded chip and the draft on sign-out', async () => {
		component = mount(HomeShell, { target: host, props: shellProps('chat') });
		await settle();
		dockBar()!.click();
		await settle();
		(host.querySelector('.other-option textarea') as HTMLTextAreaElement).value = 'private';
		host.querySelector('.other-option textarea')!
			.dispatchEvent(new Event('input', { bubbles: true }));
		await settle();
		expect(host.querySelectorAll('.decision-chip').length).toBe(1);

		token.set(null);
		await settle();

		expect(host.querySelector('[data-attention-dock]')).toBeNull();
		expect(host.querySelectorAll('.decision-chip').length).toBe(0);
	});

	it('does not let the next identity inherit the previous draft', async () => {
		component = mount(HomeShell, { target: host, props: shellProps('chat') });
		await settle();
		dockBar()!.click();
		await settle();
		const input = host.querySelector('.other-option textarea') as HTMLTextAreaElement;
		input.value = 'previous user typed this';
		input.dispatchEvent(new Event('input', { bubbles: true }));
		await settle();

		token.set(null);
		await settle();
		token.set('another-bearer');
		await settle();

		dockBar()!.click();
		await settle();
		expect((host.querySelector('.other-option textarea') as HTMLTextAreaElement).value)
			.toBe('');
	});

	it('makes a response issued before sign-out inadmissible afterwards', async () => {
		let release: (() => void) | null = null;
		queueDelay = () => new Promise<void>((resolve) => { release = resolve; });
		component = mount(HomeShell, { target: host, props: shellProps('chat') });
		await settle();

		token.set(null);
		await settle();
		release!();
		await settle();

		// The in-flight read belonged to the previous identity.
		expect(host.querySelector('[data-attention-dock]')).toBeNull();
		expect(queue).toBeTruthy();
		let count = -1;
		queue.subscribe((value) => { count = value.count; })();
		expect(count).toBe(0);
		let open: string | null = 'x';
		expandedId.subscribe((value) => { open = value; })();
		expect(open).toBeNull();
	});
});

describe('Named region and truthful indicator copy', () => {
	it('names the dock as a region, carrying the state a sighted user reads', async () => {
		component = mount(HomeShell, { target: host, props: shellProps('chat') });
		await settle();

		const region = host.querySelector('[data-attention-dock]')!;
		expect(region.getAttribute('role')).toBe('region');
		const label = region.getAttribute('aria-label') ?? '';
		expect(label).toContain('Needs you');
		expect(label).toContain('Where should decision state live?');
		expect(label).toContain('2 waiting');
		expect(region.querySelector('[aria-live="polite"]')).toBeTruthy();
	});

});
