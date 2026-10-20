import { afterEach, describe, expect, it } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';
import Avatar from './Avatar.svelte';
import AvatarStack from './AvatarStack.svelte';
import avatarStackSource from './AvatarStack.svelte?raw';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
afterEach(() => { if (component) unmount(component); component = null; host?.remove(); });
function target() { host = document.createElement('div'); document.body.appendChild(host); return host; }
describe('Avatar accessibility', () => {
	it('names a standalone avatar and hides one beside named text', () => {
		component = mount(Avatar, { target: target(), props: { name: 'Quality Gate', color: 'red' } }); flushSync();
		expect(host.querySelector('.avatar')?.getAttribute('aria-label')).toBe('Quality Gate');
		unmount(component); component = mount(Avatar, { target: host, props: { name: 'Quality Gate', color: 'red', hidden: true } }); flushSync();
		expect(host.querySelector('.avatar')?.getAttribute('aria-hidden')).toBe('true');
	});
	it('names the people summarized by +N', () => {
		component = mount(AvatarStack, { target: target(), props: { people: [{ name: 'Quality Gate', color: 'red' }, { name: 'Bugs', color: 'amber' }, { name: 'Context & Memory', color: 'blue' }, { name: 'Structured Plans', color: 'green' }] } }); flushSync();
		const more = host.querySelector('.more');
		expect(host.querySelector('.stack')?.getAttribute('role')).toBe('group');
		expect(more?.getAttribute('aria-label')).toBeNull();
		expect(more?.querySelector('.sr-only')?.textContent).toBe('and 1 more: Structured Plans');
		expect(more?.getAttribute('title')).toBe('and 1 more: Structured Plans');
	});

	it('keeps same-name participants separate by id and exposes name plus description', () => {
		component = mount(AvatarStack, { target: target(), props: { people: [
			{ id: 'agent-a', name: 'Reviewer', color: 'red', detail: 'commented 20 s ago', active: true },
			{ id: 'agent-b', name: 'Reviewer', color: 'blue', detail: 'read 3 min ago' },
		] } });
		flushSync();
		const triggers = [...host.querySelectorAll<HTMLButtonElement>('.participant-trigger')];
		expect(triggers.map((trigger) => trigger.dataset.participantId)).toEqual(['agent-a', 'agent-b']);
		expect(triggers[0].getAttribute('aria-label')).toBe('Reviewer');
		expect(triggers[0].classList.contains('arrival')).toBe(true);
		expect(triggers[1].classList.contains('arrival')).toBe(false);
		const descriptionId = triggers[0].getAttribute('aria-describedby');
		expect(descriptionId).toBeTruthy();
		expect(host.querySelector(`#${descriptionId}`)?.textContent).toBe('commented 20 s ago');
	});

	it('opens agent detail by keyboard focus and tap, then closes it on Escape', () => {
		component = mount(AvatarStack, { target: target(), props: { people: [
			{ id: 'agent-a', name: 'Reviewer', color: 'red', detail: 'commented 20 s ago' },
		] } });
		flushSync();
		const trigger = host.querySelector<HTMLButtonElement>('.participant-trigger')!;
		trigger.focus();
		flushSync();
		expect(document.body.querySelector('[role="dialog"]')?.textContent).toContain('commented 20 s ago');
		window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
		flushSync();
		expect(document.body.querySelector('[role="dialog"]')).toBeNull();
		trigger.click();
		flushSync();
		expect(document.body.querySelector('[role="dialog"]')).not.toBeNull();
	});

	it('opens detail on hover and closes it when the pointer leaves', () => {
		component = mount(AvatarStack, { target: target(), props: { people: [
			{ id: 'agent-a', name: 'Reviewer', color: 'red', detail: 'commented 20 s ago' },
		] } });
		flushSync();
		const trigger = host.querySelector<HTMLButtonElement>('.participant-trigger')!;
		trigger.dispatchEvent(new MouseEvent('mouseenter'));
		flushSync();
		expect(document.body.querySelector('[role="dialog"]')?.textContent).toContain('commented 20 s ago');
		trigger.dispatchEvent(new MouseEvent('mouseleave'));
		flushSync();
		expect(document.body.querySelector('[role="dialog"]')).toBeNull();
	});

	it('closes detail when focus moves outside the stack', () => {
		component = mount(AvatarStack, { target: target(), props: { people: [
			{ id: 'agent-a', name: 'Reviewer', color: 'red', detail: 'commented 20 s ago' },
		] } });
		flushSync();
		const trigger = host.querySelector<HTMLButtonElement>('.participant-trigger')!;
		const outside = document.createElement('button');
		document.body.appendChild(outside);
		trigger.focus();
		flushSync();
		expect(document.body.querySelector('[role="dialog"]')).not.toBeNull();
		trigger.dispatchEvent(new FocusEvent('focusout', { bubbles: true, relatedTarget: outside }));
		flushSync();
		expect(document.body.querySelector('[role="dialog"]')).toBeNull();
		outside.remove();
	});

	it('keeps detail-avatar overlap on the stack items, never the open popover', () => {
		const overlap = '.stack > .participant-trigger, .stack > .participant-slot, .stack > .more';
		component = mount(AvatarStack, { target: target(), props: { people: [
			{ id: 'agent-a', name: 'Reviewer', color: 'red', detail: 'commented 20 s ago' },
			{ id: 'agent-b', name: 'Writer', color: 'blue', detail: 'read 3 min ago' },
			{ name: 'Plain', color: 'green' },
		] } });
		flushSync();
		const triggers = host.querySelectorAll<HTMLElement>('.participant-trigger');
		triggers[0].focus();
		flushSync();
		const dialog = document.body.querySelector<HTMLElement>('[role="dialog"]');
		expect(dialog).not.toBeNull();
		// The popover is portaled to `document.body`, so the stack's overlap selector can never reach it.
		expect(dialog!.parentElement).toBe(document.body);
		expect(dialog!.closest('.stack')).toBeNull();
		expect(dialog!.matches(overlap)).toBe(false);
		expect(triggers[1].matches(overlap)).toBe(true);
		expect(host.querySelector<HTMLElement>('.stack > .participant-slot')?.matches(overlap)).toBe(true);
		expect(avatarStackSource).toContain(
			'.stack > .participant-trigger, .stack > .participant-slot, .stack > .more { margin-left: -5px; }',
		);
		expect(avatarStackSource).toContain('.stack > :first-child { margin-left: 0; }');
	});

	it('names the reader in the detail, not only in the accessible label', () => {
		component = mount(AvatarStack, { target: target(), props: { people: [
			{ id: 'agent-a', name: 'Product Experience', color: 'red', detail: 'read 2 min ago' },
		] } });
		flushSync();
		host.querySelector<HTMLButtonElement>('.participant-trigger')!.focus();
		flushSync();
		expect(document.body.querySelector('[role="dialog"]')?.textContent).toBe('Product Experience · read 2 min ago');
	});

	it('gives plain and detail avatars the same phone slot so the stack spaces evenly', () => {
		component = mount(AvatarStack, { target: target(), props: { people: [
			{ name: 'Steve', color: 'green' },
			{ id: 'agent-a', name: 'Reviewer', color: 'red', detail: 'read 2 min ago' },
		] } });
		flushSync();
		expect(host.querySelector('.stack > .participant-slot > .avatar')).not.toBeNull();
		expect(avatarStackSource).toContain(
			'.participant-trigger, .participant-slot { min-width: 40px; min-height: 40px; align-items: center; justify-content: center; margin-left: -9px; }',
		);
		expect(avatarStackSource).toContain('.participant-trigger:first-child, .participant-slot:first-child { margin-left: 0; }');
	});

	it('keeps the tap detail inside a narrow viewport', async () => {
		Object.defineProperty(window, 'innerWidth', { configurable: true, value: 320 });
		component = mount(AvatarStack, { target: target(), props: { people: [
			{ id: 'agent-a', name: 'Reviewer', color: 'red', detail: 'edited 10 s ago' },
		] } });
		flushSync();
		host.querySelector<HTMLButtonElement>('.participant-trigger')!.click();
		flushSync();
		await tick();
		const dialog = document.body.querySelector<HTMLElement>('[role="dialog"]');
		expect(dialog?.textContent).toContain('edited 10 s ago');
		expect(window.innerWidth).toBe(320);
	});
});
