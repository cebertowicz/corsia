import { describe, expect, it } from 'vitest';
import { Decoration } from '@codemirror/view';
import { BlockWidget, priorWidget } from './block-widget';

class Block extends BlockWidget {
	constructor(readonly height: number, prior: Block | null = null) { super(prior); }
	toDOM() {
		const el = document.createElement('div');
		// jsdom lays nothing out: give the element the height a browser would have drawn it at.
		Object.defineProperty(el, 'offsetHeight', { value: this.height });
		document.body.append(el);
		return this.drew(el);
	}
}

describe('BlockWidget', () => {
	it('starts a replacement at the height its predecessor was drawn at, not one line', () => {
		const drawn = new Block(480);
		drawn.toDOM();
		expect(new Block(0).estimatedHeight).toBe(-1);
		expect(new Block(0, drawn).estimatedHeight).toBe(480);
	});

	it('carries the drawn height through replacements that were never drawn, and keeps only the drawn one', () => {
		const drawn = new Block(480);
		drawn.toDOM();
		let latest = new Block(0, drawn);
		for (let i = 0; i < 5; i++) latest = new Block(0, latest);
		expect(latest.estimatedHeight).toBe(480);
		expect((latest as unknown as { prior: Block }).prior).toBe(drawn);
	});

	it('measures itself once drawn, and gives no estimate when drawn into nothing laid out', () => {
		const drawn = new Block(480);
		drawn.toDOM();
		const next = new Block(510, drawn);
		next.toDOM();
		expect(next.estimatedHeight).toBe(510);
		expect(new Block(0, new Block(0)).estimatedHeight).toBe(-1);
	});

	it('finds the predecessor of the same kind over the range', () => {
		const table = new Block(300);
		const set = Decoration.set([Decoration.replace({ widget: table, block: true }).range(10, 40)]);
		expect(priorWidget(set, 10, 45, Block)).toBe(table);
		expect(priorWidget(set, 50, 60, Block)).toBeNull();
		expect(priorWidget(undefined, 10, 40, Block)).toBeNull();
	});
});
