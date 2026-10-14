import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import BrowserLeaf from './BrowserLeaf.svelte';

class FakeSocket {
	static CONNECTING = 0;
	static OPEN = 1;
	static CLOSED = 3;
	static instances: FakeSocket[] = [];
	readyState = FakeSocket.CONNECTING;
	onopen: (() => void) | null = null;
	onmessage: ((event: { data: string }) => void) | null = null;
	onclose: (() => void) | null = null;
	sent: Record<string, unknown>[] = [];
	constructor(_url: string) { FakeSocket.instances.push(this); }
	send(raw: string) { this.sent.push(JSON.parse(raw)); }
	close() { this.readyState = FakeSocket.CLOSED; this.onclose?.(); }
	open() { this.readyState = FakeSocket.OPEN; this.onopen?.(); }
	receive(message: Record<string, unknown>) { this.onmessage?.({ data: JSON.stringify(message) }); }
}

class TestPointerEvent extends MouseEvent {
	pointerId: number;
	pointerType: string;
	isPrimary = true;
	constructor(type: string, init: PointerEventInit = {}) {
		super(type, init);
		this.pointerId = init.pointerId ?? 1;
		this.pointerType = init.pointerType ?? 'mouse';
	}
}

let host: HTMLDivElement;
let component: ReturnType<typeof mount> | null = null;

function pointer(type: string, x: number, y: number, pointerType = 'mouse') {
	return new TestPointerEvent(type, { bubbles: true, cancelable: true, clientX: x, clientY: y, pointerId: 1, pointerType });
}

function render() {
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(BrowserLeaf, { target: host, props: { threadId: 'thread' } });
	flushSync();
	const socket = FakeSocket.instances[0];
	socket.open();
	socket.receive({ type: 'control', has_control: true });
	socket.receive({ type: 'frame', metadata: { deviceWidth: 100, deviceHeight: 50 }, data: '' });
	return { canvas: host.querySelector<HTMLCanvasElement>('canvas')!, socket };
}

beforeEach(() => {
	FakeSocket.instances = [];
	vi.stubGlobal('WebSocket', FakeSocket);
	vi.stubGlobal('PointerEvent', TestPointerEvent);
	vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
	vi.stubGlobal('Image', class { naturalWidth = 100; naturalHeight = 50; src = ''; decode() { return Promise.resolve(); } });
	vi.spyOn(HTMLCanvasElement.prototype, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 0, 100, 100));
	vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ setTransform() {}, drawImage() {} } as unknown as CanvasRenderingContext2D);
	Object.defineProperties(HTMLCanvasElement.prototype, {
		setPointerCapture: { configurable: true, value() {} },
		releasePointerCapture: { configurable: true, value() {} },
		hasPointerCapture: { configurable: true, value: () => true },
	});
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host?.remove();
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
});

describe('BrowserLeaf letterbox input', () => {
	it('ignores mouse and touch presses outside the drawn page', () => {
		const { canvas, socket } = render();
		const before = socket.sent.length;
		canvas.dispatchEvent(pointer('pointerdown', 50, 10));
		canvas.dispatchEvent(pointer('pointerdown', 50, 10, 'touch'));
		expect(socket.sent.slice(before)).toEqual([]);
	});

	it('releases an inside press at the clamped page edge', () => {
		const { canvas, socket } = render();
		canvas.dispatchEvent(pointer('pointerdown', 50, 50));
		canvas.dispatchEvent(pointer('pointerup', 50, 110));
		expect(socket.sent.filter(message => message.type === 'input_mouse')).toEqual([
			{ type: 'input_mouse', eventType: 'mouseMoved', x: 50, y: 25, button: 'none', clickCount: 0 },
			{ type: 'input_mouse', eventType: 'mousePressed', x: 50, y: 25, button: 'left', clickCount: 1 },
			{ type: 'input_mouse', eventType: 'mouseReleased', x: 50, y: 50, button: 'left', clickCount: 1 },
		]);
	});
});
