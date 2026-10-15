import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import BrowserLeaf from './BrowserLeaf.svelte';
import ArtifactLeaf from './ArtifactLeaf.svelte';
import { emit } from '$lib/ws/events';
import { get } from 'svelte/store';
import { openBrowserPane, threadWorkspaces } from '$lib/stores/artifactWorkspace';

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
let drawing: string[] = [];

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
	threadWorkspaces.set({});
	vi.stubGlobal('WebSocket', FakeSocket);
	vi.stubGlobal('PointerEvent', TestPointerEvent);
	vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
	vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true }));
	vi.stubGlobal('Image', class { naturalWidth = 100; naturalHeight = 50; src = ''; decode() { return Promise.resolve(); } });
	vi.spyOn(HTMLCanvasElement.prototype, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 0, 100, 100));
	drawing = [];
	vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
		setTransform() {},
		clearRect(...area: number[]) { drawing.push(`clear ${area.join(',')}`); },
		drawImage(_image: unknown, ...area: number[]) { drawing.push(`draw ${area.slice(4).join(',')}`); },
	} as unknown as CanvasRenderingContext2D);
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

describe('BrowserLeaf drawing', () => {
	it('clears the old letterbox when the page size changes inside an unchanged pane', async () => {
		const { socket } = render();
		await vi.waitFor(() => expect(drawing).toContain('draw 0,25,100,50'));
		drawing = [];
		socket.receive({ type: 'frame', metadata: { deviceWidth: 100, deviceHeight: 50 }, data: '' });
		await vi.waitFor(() => expect(drawing).toEqual(['draw 0,25,100,50']));
		drawing = [];
		socket.receive({ type: 'frame', metadata: { deviceWidth: 50, deviceHeight: 50 }, data: '' });
		await vi.waitFor(() => expect(drawing).toEqual(['clear 0,0,100,100', 'draw 0,0,100,100']));
	});
});

describe('BrowserLeaf navigation metadata', () => {
	it('shows an address led by the registrable domain after navigation', () => {
		const { socket } = render();
		socket.receive({ type: 'navigate_result', error: null, url: 'https://accounts.example.co.uk/a/long/page', title: 'Example page' });
		flushSync();
		expect(host.querySelector<HTMLInputElement>('[aria-label="Browser address"]')?.value).toBe('example.co.uk · accounts · …page');
	});

	it('updates the address and tab when an agent-side navigation reaches the stream', () => {
		openBrowserPane('thread');
		const { socket } = render();
		socket.receive({ type: 'browser_metadata', url: 'https://www.example.test/agent/open', title: 'Agent opened this' });
		flushSync();
		expect(host.querySelector<HTMLInputElement>('[aria-label="Browser address"]')?.value).toBe('example.test · www · …open');
		expect(get(threadWorkspaces).thread.tabs.find((tab) => tab.kind === 'browser')?.title).toBe('Agent opened this');
		expect(FakeSocket.instances).toEqual([socket]);
		expect(socket.readyState).not.toBe(FakeSocket.CLOSED);
	});

	it('keeps its stream connected when page metadata relabels the browser tab', () => {
		openBrowserPane('thread');
		host = document.createElement('div');
		document.body.appendChild(host);
		component = mount(ArtifactLeaf, { target: host, props: { threadId: 'thread', mode: 'expanded' } });
		flushSync();
		const socket = FakeSocket.instances[0];
		socket.open();
		socket.receive({ type: 'browser_metadata', url: 'https://www.example.test/agent/open', title: 'Agent opened this' });
		flushSync();
		expect(get(threadWorkspaces).thread.tabs.find((tab) => tab.kind === 'browser')?.title).toBe('Agent opened this');
		expect(FakeSocket.instances).toEqual([socket]);
		expect(socket.readyState).not.toBe(FakeSocket.CLOSED);
	});

	it('submits exactly the URL typed after an agent navigation', () => {
		const { socket } = render();
		socket.receive({ type: 'browser_metadata', url: 'https://www.example.test/agent/open', title: 'Agent opened this' });
		const input = host.querySelector<HTMLInputElement>('[aria-label="Browser address"]')!;
		input.dispatchEvent(new FocusEvent('focus'));
		input.value = 'https://next.example.test/person/page';
		input.dispatchEvent(new Event('input', { bubbles: true }));
		host.querySelector('form')!.dispatchEvent(new SubmitEvent('submit', { bubbles: true, cancelable: true }));
		expect(socket.sent.at(-1)).toEqual({ type: 'navigate', url: 'https://next.example.test/person/page' });
	});

	it('replaces the full address when a selection made on focus is typed over before the next render', () => {
		const { socket } = render();
		socket.receive({ type: 'browser_metadata', url: 'https://www.example.test/agent/open', title: 'Agent opened this' });
		flushSync();
		const input = host.querySelector<HTMLInputElement>('[aria-label="Browser address"]')!;
		input.dispatchEvent(new FocusEvent('focus'));
		input.select();
		flushSync();
		input.setRangeText('https://next.example.test/person/page', input.selectionStart!, input.selectionEnd!, 'end');
		input.dispatchEvent(new Event('input', { bubbles: true }));
		host.querySelector('form')!.dispatchEvent(new SubmitEvent('submit', { bubbles: true, cancelable: true }));
		expect(socket.sent.at(-1)).toEqual({ type: 'navigate', url: 'https://next.example.test/person/page' });
	});
});

describe('BrowserLeaf opening lifecycle', () => {
	it('sends appearance after the first layout when the socket opens before it', () => {
		let afterLayout: FrameRequestCallback | undefined;
		vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
			afterLayout = callback;
			return 1;
		});
		vi.spyOn(HTMLCanvasElement.prototype, 'getBoundingClientRect')
			.mockReturnValueOnce(new DOMRect(0, 0, 0, 0))
			.mockReturnValue(new DOMRect(0, 0, 100, 100));
		host = document.createElement('div');
		document.body.appendChild(host);
		component = mount(BrowserLeaf, { target: host, props: { threadId: 'thread' } });
		flushSync();
		const socket = FakeSocket.instances[0];
		socket.open();
		expect(socket.sent.filter(message => message.type === 'appearance')).toEqual([]);
		afterLayout?.(0);
		expect(socket.sent).toContainEqual({ type: 'appearance', width: 100, height: 100, scale: 1, scheme: 'dark' });
	});

	async function expectOpen(url: string) {
		await vi.waitFor(() => expect(fetch).toHaveBeenCalledWith(`/api/browser/thread/open?url=${encodeURIComponent(url)}`, {
			method: 'POST', headers: { Authorization: 'Bearer ' }
		}));
	}

	function submit(url: string) {
		const input = host.querySelector<HTMLInputElement>('[aria-label="Browser address"]')!;
		input.dispatchEvent(new FocusEvent('focus'));
		input.value = url;
		input.dispatchEvent(new Event('input', { bubbles: true }));
		host.querySelector<HTMLButtonElement>('button[type="submit"]')!.click();
	}

	it('opens over HTTP when Go is clicked while the stream socket is connecting', async () => {
		host = document.createElement('div');
		document.body.appendChild(host);
		component = mount(BrowserLeaf, { target: host, props: { threadId: 'thread' } });
		flushSync();
		submit('https://example.test/connecting');
		await expectOpen('https://example.test/connecting');
	});

	it('opens over HTTP after the stream reports no browser yet', async () => {
		const { socket } = render();
		socket.receive({ type: 'browser_state', state: 'no_browser_yet' });
		submit('https://example.test/no-browser');
		await expectOpen('https://example.test/no-browser');
	});
});

describe('BrowserLeaf keyboard relay', () => {
	it('preserves the modifier bits required by page shortcuts', () => {
		const { canvas, socket } = render();
		canvas.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, cancelable: true, key: 'Meta', code: 'MetaLeft', metaKey: true }));
		canvas.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, cancelable: true, key: 'a', code: 'KeyA', metaKey: true }));
		expect(socket.sent.filter(message => message.type === 'input_keyboard')).toEqual([
			{ type: 'input_keyboard', eventType: 'keyDown', key: 'Meta', code: 'MetaLeft', keyCode: 0, text: '', modifiers: 4 },
			{ type: 'input_keyboard', eventType: 'keyDown', key: 'a', code: 'KeyA', keyCode: 0, text: '', modifiers: 4 },
		]);
	});
});

describe('BrowserLeaf reconnect', () => {
	it('reconnects an ended stream when the agent opens its browser again', () => {
		const { socket } = render();
		socket.close();
		emit('browser_opened', { thread_id: 'thread' });
		expect(FakeSocket.instances).toHaveLength(2);
		emit('browser_opened', { thread_id: 'thread' });
		expect(FakeSocket.instances).toHaveLength(2);
	});
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

	it('forwards wheel axes and mode without turning vertical input horizontal', () => {
		const { canvas, socket } = render();
		canvas.dispatchEvent(new WheelEvent('wheel', { bubbles: true, cancelable: true, clientX: 50, clientY: 50, deltaX: 3, deltaY: 90, deltaMode: 0 }));
		expect(socket.sent.at(-1)).toEqual({ type: 'input_mouse', eventType: 'mouseWheel', x: 50, y: 25, deltaX: 3, deltaY: 90, deltaMode: 0 });
	});
});
