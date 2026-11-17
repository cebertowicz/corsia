import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import BrowserLeaf from './BrowserLeaf.svelte';
import { threadWorkspaces } from '$lib/stores/artifactWorkspace';

class FakeSocket {
	static instances: FakeSocket[] = [];
	readyState = 0;
	bufferedAmount = 0;
	onopen: (() => void) | null = null;
	onmessage: ((event: { data: string }) => void) | null = null;
	onclose: (() => void) | null = null;
	sent: Record<string, unknown>[] = [];
	constructor(_url: string) { FakeSocket.instances.push(this); }
	send(raw: string) { this.sent.push(JSON.parse(raw)); }
	close() { this.readyState = 3; this.onclose?.(); }
	open() { this.readyState = 1; this.onopen?.(); }
	receive(message: Record<string, unknown>) { this.onmessage?.({ data: JSON.stringify(message) }); }
}

class TestPointerEvent extends MouseEvent {
	pointerId = 1;
	pointerType = 'mouse';
	isPrimary = true;
}

let host: HTMLDivElement;
let component: ReturnType<typeof mount> | null = null;
const frame = (extra: Record<string, unknown> = {}) => ({ type: 'frame', metadata: { deviceWidth: 100, deviceHeight: 50 }, data: '', ...extra });
const reports = (socket: FakeSocket) => socket.sent.filter(message => message.type === 'pane_stats');
const total = (buckets: unknown) => (buckets as number[]).reduce((sum, count) => sum + count, 0);
// A frame is painted once its decode promise settles.
const settle = () => new Promise<void>(resolve => setTimeout(resolve, 0));

function render() {
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(BrowserLeaf, { target: host, props: { threadId: 'thread' } });
	flushSync();
	const socket = FakeSocket.instances.at(-1)!;
	socket.open();
	socket.receive({ type: 'browser_metadata', url: 'https://example.test', title: 'Example', domain: 'example.test' });
	return { canvas: host.querySelector<HTMLCanvasElement>('canvas')!, socket };
}

beforeEach(() => {
	FakeSocket.instances = [];
	threadWorkspaces.set({});
	vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
	vi.stubGlobal('WebSocket', Object.assign(FakeSocket, { OPEN: 1, CLOSED: 3, CONNECTING: 0 }));
	vi.stubGlobal('PointerEvent', TestPointerEvent);
	vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
	vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true }));
	vi.stubGlobal('Image', class { naturalWidth = 100; naturalHeight = 50; src = ''; decode() { return Promise.resolve(); } });
	vi.spyOn(HTMLCanvasElement.prototype, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 0, 100, 100));
	vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
		setTransform() {}, save() {}, restore() {}, beginPath() {}, rect() {}, clip() {}, clearRect() {}, drawImage() {}
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
	vi.useRealTimers();
});

describe('BrowserLeaf stream stats', () => {
	it('reports what it painted every ten seconds, and the last frame number', async () => {
		const { socket } = render();
		socket.receive(frame({ seq: 3 }));
		await settle();
		vi.advanceTimersByTime(10_000);
		expect(reports(socket)[0]).toMatchObject({ painted: 1, painted_seq: 3, visible: true });
	});

	it('numbers a click and times it against the frame that names it', async () => {
		const { canvas, socket } = render();
		socket.receive(frame());
		await settle();
		canvas.dispatchEvent(new TestPointerEvent('pointerdown', { bubbles: true, cancelable: true, clientX: 50, clientY: 50 }));
		expect(socket.sent.find(message => message.eventType === 'mousePressed')).toMatchObject({ id: 1 });
		expect(socket.sent.find(message => message.eventType === 'mouseMoved')).not.toHaveProperty('id');
		socket.receive(frame({ seq: 1, input: 1 }));
		await settle();
		vi.advanceTimersByTime(10_000);
		expect(total(reports(socket).at(-1)?.input_to_paint_ms)).toBe(1);
	});

	it('stops reporting when the stream ends and numbers a new connection from one', async () => {
		const { socket } = render();
		socket.receive(frame({ seq: 2 }));
		await settle();
		vi.advanceTimersByTime(10_000);
		const before = reports(socket).length;
		expect(before).toBe(1);
		socket.close();
		vi.advanceTimersByTime(60_000);
		expect(reports(socket)).toHaveLength(before);
		unmount(component!);
		const next = render();
		next.canvas.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, cancelable: true, key: 'a', code: 'KeyA' }));
		expect(next.socket.sent.find(message => message.type === 'input_keyboard')).toMatchObject({ id: 1 });
	});
});
