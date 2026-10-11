import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { press } from './press';

function pressable(): { button: HTMLButtonElement; destroy: () => void } {
	const button = document.createElement('button');
	document.body.appendChild(button);
	const action = press(button);
	return { button, destroy: () => { action.destroy(); button.remove(); } };
}

describe('press', () => {
	it('marks the control and tints it only while a pointer is down', () => {
		const { button, destroy } = pressable();
		expect(button.hasAttribute('data-press')).toBe(true);
		expect(button.hasAttribute('data-pressing')).toBe(false);
		button.dispatchEvent(new Event('pointerdown'));
		expect(button.hasAttribute('data-pressing')).toBe(true);
		button.dispatchEvent(new Event('pointerup'));
		expect(button.hasAttribute('data-pressing')).toBe(false);
		destroy();
	});

	it('releases on cancel and when the pointer leaves', () => {
		const { button, destroy } = pressable();
		for (const type of ['pointercancel', 'pointerleave']) {
			button.dispatchEvent(new Event('pointerdown'));
			button.dispatchEvent(new Event(type));
			expect(button.hasAttribute('data-pressing')).toBe(false);
		}
		destroy();
	});

	it('stops reacting once destroyed', () => {
		const { button, destroy } = pressable();
		destroy();
		button.dispatchEvent(new Event('pointerdown'));
		expect(button.hasAttribute('data-pressing')).toBe(false);
	});

	it('anchors the tint overlay on a static control', () => {
		const computed = window.getComputedStyle;
		window.getComputedStyle = ((node: Element) => ({ ...computed(node), position: 'static' })) as typeof window.getComputedStyle;
		const { button, destroy } = pressable();
		window.getComputedStyle = computed;
		expect(button.style.position).toBe('relative');
		destroy();
	});

	it('tints with opacity only: no size change, and it replaces the global press scale', () => {
		const css = readFileSync(join(process.cwd(), 'src/lib/components/ui/press.css'), 'utf8');
		expect(css).toContain('[data-press][data-pressing]::after { opacity: 1; }');
		expect(css).toContain('scale: none');
		expect(css).not.toMatch(/transform\s*:/);
	});
});
