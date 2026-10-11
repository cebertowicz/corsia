import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createRawSnippet, flushSync, mount, unmount } from 'svelte';
import Expand from './Expand.svelte';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
const children = createRawSnippet(() => ({ render: () => '<p class="body">content</p>' }));

function stubMotion(reduced: boolean): void {
	vi.stubGlobal('matchMedia', (query: string) => ({ matches: reduced && query.includes('reduce'), media: query, addEventListener() {}, removeEventListener() {} }));
}

function render(props: { open: boolean; keep?: boolean }) {
	let state = $state({ ...props, present: props.open });
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(Expand, {
		target: host,
		props: {
			get open() { return state.open; },
			get keep() { return state.keep; },
			get present() { return state.present; },
			set present(value: boolean) { state.present = value; },
			children,
		},
	});
	flushSync();
	return {
		set(open: boolean) { state.open = open; flushSync(); },
		get present() { return state.present; },
	};
}

const region = () => host.querySelector<HTMLElement>('.expand');

beforeEach(() => { vi.useFakeTimers(); stubMotion(false); });
afterEach(() => {
	if (component) unmount(component);
	component = null;
	host?.remove();
	vi.useRealTimers();
	vi.unstubAllGlobals();
});

describe('Expand', () => {
	it('renders nothing while closed', () => {
		render({ open: false });
		expect(region()).toBeNull();
	});

	it('is open without an entrance when it starts open', () => {
		const handle = render({ open: true });
		expect(region()!.classList.contains('open')).toBe(true);
		expect(handle.present).toBe(true);
	});

	it('mounts closed, then opens on the next frame', async () => {
		const handle = render({ open: false });
		handle.set(true);
		expect(handle.present).toBe(true);
		expect(region()!.classList.contains('open')).toBe(false);
		await vi.advanceTimersByTimeAsync(0);
		flushSync();
		expect(region()!.classList.contains('open')).toBe(true);
		await vi.advanceTimersByTimeAsync(400);
		flushSync();
		expect(region()!.classList.contains('settled')).toBe(true);
	});

	it('stays mounted while it closes, then unmounts and reports it', async () => {
		const handle = render({ open: true });
		handle.set(false);
		expect(region()).not.toBeNull();
		expect(region()!.classList.contains('open')).toBe(false);
		expect((region() as HTMLElement & { inert: boolean }).inert).toBe(true);
		expect(handle.present).toBe(true);
		await vi.advanceTimersByTimeAsync(400);
		flushSync();
		expect(handle.present).toBe(false);
		expect(region()).toBeNull();
	});

	it('keeps its content rendered while closed when asked to', () => {
		render({ open: false, keep: true });
		expect(host.querySelector('.body')).not.toBeNull();
		expect((region() as HTMLElement & { inert: boolean }).inert).toBe(true);
	});

	it('does not unmount if it is reopened mid-close', async () => {
		const handle = render({ open: true });
		handle.set(false);
		await vi.advanceTimersByTimeAsync(60);
		handle.set(true);
		await vi.advanceTimersByTimeAsync(600);
		flushSync();
		expect(region()).not.toBeNull();
		expect(region()!.classList.contains('open')).toBe(true);
		expect(handle.present).toBe(true);
	});

	it('finishes a reduced-motion close after the short crossfade, with no size transition', async () => {
		stubMotion(true);
		const handle = render({ open: true });
		handle.set(false);
		await vi.advanceTimersByTimeAsync(240);
		flushSync();
		expect(handle.present).toBe(false);
		expect(region()).toBeNull();
	});

	it('caps its column at the parent width so a settled region cannot grow around nowrap content', () => {
		const source = readFileSync(join(process.cwd(), 'src/lib/components/ui/Expand.svelte'), 'utf8');
		expect(source).toContain('grid-template-columns: minmax(0, 1fr)');
		expect(source).toMatch(/\.expand-inner \{[^}]*min-width: 0/);
	});

	it('has no size transition under reduced motion, only an opacity crossfade', () => {
		const source = readFileSync(join(process.cwd(), 'src/lib/components/ui/Expand.svelte'), 'utf8');
		const reduced = source.slice(source.indexOf('@media (prefers-reduced-motion: reduce)'));
		expect(reduced).toContain('.expand, .expand.open { transition: none; }');
		expect(reduced).toContain('transition: opacity var(--dur-tight) var(--ease-snap) !important');
		expect(reduced).not.toContain('grid-template-rows');
	});
});
