import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { portal } from './portal';

let host: HTMLElement;

beforeEach(() => {
	document.body.innerHTML = '';
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	document.body.innerHTML = '';
});

describe('portal', () => {
	it('moves the node to document.body by default', () => {
		const node = document.createElement('div');
		host.appendChild(node);

		const action = portal(node);

		expect(node.parentNode).toBe(document.body);
		expect(host.contains(node)).toBe(false);
		action.destroy();
	});

	it('moves the node to an explicit target element', () => {
		const other = document.createElement('div');
		document.body.appendChild(other);
		const node = document.createElement('div');
		host.appendChild(node);

		const action = portal(node, other);

		expect(node.parentNode).toBe(other);
		action.destroy();
	});

	it('removes the node from its portal target on destroy', () => {
		const node = document.createElement('div');
		host.appendChild(node);

		const action = portal(node);
		expect(document.body.contains(node)).toBe(true);

		action.destroy();
		expect(document.body.contains(node)).toBe(false);
	});
});
