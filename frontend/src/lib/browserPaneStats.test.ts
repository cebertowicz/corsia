import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { BrowserPaneStats, STATS_BUCKET_UPPERS } from './browserPaneStats';

let clock = 0;
let queued = 0;
let visible = true;
const stats = () => new BrowserPaneStats(() => queued, () => clock, () => visible);
const bucket = (ms: number) => STATS_BUCKET_UPPERS.findIndex(upper => ms <= upper);
const total = (buckets: unknown) => (buckets as number[]).reduce((sum, count) => sum + count, 0);

afterEach(() => {
	clock = 0;
	queued = 0;
	visible = true;
	vi.useRealTimers();
});

describe('BrowserPaneStats', () => {
	it('uses the backend histogram buckets, so the server can add the pane counts to its own', () => {
		const source = readFileSync(resolve(process.cwd(), '../backend/infra/request_timing.py'), 'utf8');
		const declared = /_BUCKET_UPPERS = \(([^)]*)\)/.exec(source)![1].split(',').map(part => part.replace(/[^\d_]/g, '').replace(/_/g, '')).filter(Boolean).map(Number);
		expect([...STATS_BUCKET_UPPERS]).toEqual(declared);
	});

	it('numbers input that can change the picture and leaves a pointer move alone', () => {
		const pane = stats();
		const move = { type: 'input_mouse', eventType: 'mouseMoved', x: 1, y: 2 };
		expect(pane.stamp(move)).toBe(move);
		expect(pane.stamp({ type: 'input_mouse', eventType: 'mousePressed' })).toEqual({ type: 'input_mouse', eventType: 'mousePressed', id: 1 });
		expect(pane.stamp({ type: 'input_mouse', eventType: 'mouseWheel', deltaY: 3 })).toMatchObject({ id: 2 });
		expect(pane.stamp({ type: 'input_keyboard', eventType: 'keyDown' })).toMatchObject({ id: 3 });
		expect(pane.stamp({ type: 'navigate', url: 'https://example.test' })).not.toHaveProperty('id');
		expect(pane.stamp({ type: 'clipboard_undo' })).not.toHaveProperty('id');
	});

	it('times an input from its send to the painted frame that names it', () => {
		const pane = stats();
		pane.stamp({ type: 'input_keyboard', eventType: 'keyDown' });
		clock = 100;
		const painted = pane.arrived({ seq: 7, input: 1 });
		clock = 130;
		painted(true);
		const report = pane.report();
		expect(report).toMatchObject({ type: 'pane_stats', painted: 1, painted_seq: 7, visible: true });
		expect((report.input_to_paint_ms as number[])[bucket(130)]).toBe(1);
		expect((report.decode_ms as number[])[bucket(30)]).toBe(1);
	});

	it('lets a replaced frame pass its input name to the next painted frame', () => {
		const pane = stats();
		pane.stamp({ type: 'input_keyboard', eventType: 'keyDown' });
		clock = 40;
		pane.arrived({ seq: 1, input: 1 })(false);
		clock = 90;
		pane.arrived({ seq: 2 })(true);
		const report = pane.report();
		expect(report).toMatchObject({ painted: 1, painted_seq: 2 });
		expect((report.input_to_paint_ms as number[])[bucket(90)]).toBe(1);
	});

	it('forgets earlier inputs once a later one is answered, and times each only once', () => {
		const pane = stats();
		pane.stamp({ type: 'input_mouse', eventType: 'mousePressed' });
		pane.stamp({ type: 'input_mouse', eventType: 'mouseReleased' });
		pane.arrived({ seq: 1, input: 2 })(true);
		pane.arrived({ seq: 2, input: 1 })(true);
		pane.arrived({ seq: 3, input: 2 })(true);
		expect(total(pane.report().input_to_paint_ms)).toBe(1);
	});

	it('times the oldest input a paint answers, so later input cannot hide how long the first waited', () => {
		const pane = stats();
		pane.stamp({ type: 'input_mouse', eventType: 'mouseWheel', deltaY: 1 });
		clock = 50;
		pane.stamp({ type: 'input_mouse', eventType: 'mouseWheel', deltaY: 1 });
		clock = 90;
		pane.stamp({ type: 'input_mouse', eventType: 'mouseWheel', deltaY: 1 });
		clock = 100;
		const painted = pane.arrived({ seq: 1, input: 3 });
		clock = 130;
		painted(true);
		const delay = pane.report().input_to_paint_ms as number[];
		expect(total(delay)).toBe(1);
		expect(delay[bucket(130)]).toBe(1);
	});

	it('keeps an input waiting until a painted frame names it or a later input', () => {
		const pane = stats();
		pane.stamp({ type: 'input_mouse', eventType: 'mouseWheel', deltaY: 1 });
		clock = 60;
		pane.arrived({ seq: 1 })(true);
		expect(total(pane.report().input_to_paint_ms)).toBe(0);
		clock = 200;
		pane.stamp({ type: 'input_mouse', eventType: 'mouseWheel', deltaY: 1 });
		clock = 300;
		pane.arrived({ seq: 2, input: 2 })(true);
		const delay = pane.report().input_to_paint_ms as number[];
		expect(delay[bucket(300)]).toBe(1);
	});

	it('ignores a decode that finishes after the connection was reset', () => {
		const pane = stats();
		pane.stamp({ type: 'input_keyboard', eventType: 'keyDown' });
		const oldPainted = pane.arrived({ seq: 900, input: 1 });
		pane.stop();
		pane.stamp({ type: 'input_keyboard', eventType: 'keyDown' });
		oldPainted(true);
		pane.arrived({ seq: 1 })(false);
		const report = pane.report();
		expect(report).toMatchObject({ painted: 0, painted_seq: 0 });
		expect(total(report.input_to_paint_ms)).toBe(0);
		// The new connection's input 1 is still pending, and its own frame answers it.
		pane.arrived({ seq: 2, input: 1 })(true);
		const answered = pane.report();
		expect(answered).toMatchObject({ painted: 1, painted_seq: 2 });
		expect(total(answered.input_to_paint_ms)).toBe(1);
	});

	it('keeps at most 64 inputs that no frame has named', () => {
		const pane = stats();
		for (let count = 0; count < 100; count++) pane.stamp({ type: 'input_keyboard', eventType: 'keyDown' });
		pane.arrived({ seq: 1, input: 1 })(true);
		expect(total(pane.report().input_to_paint_ms)).toBe(0);
		pane.arrived({ seq: 2, input: 100 })(true);
		expect(total(pane.report().input_to_paint_ms)).toBe(1);
	});

	it('reports whole-number counts in the backend histogram shape, then starts the interval again', () => {
		const pane = stats();
		queued = 4096.4;
		visible = false;
		pane.stamp({ type: 'input_keyboard', eventType: 'keyDown' });
		queued = 0;
		pane.arrived({ seq: 9 })(true);
		const first = pane.report();
		expect(first).toMatchObject({ painted: 1, painted_seq: 9, up_buffered_max: 4096, visible: false });
		expect(first.decode_ms).toHaveLength(STATS_BUCKET_UPPERS.length);
		expect(first.input_to_paint_ms).toHaveLength(STATS_BUCKET_UPPERS.length);
		// The newest painted number keeps counting across intervals; the counts do not.
		expect(pane.report()).toMatchObject({ painted: 0, painted_seq: 9, up_buffered_max: 0 });
	});

	it('sends a report every ten seconds until stopped, then numbers from one again', () => {
		vi.useFakeTimers();
		const pane = stats();
		const sent: Record<string, unknown>[] = [];
		pane.start(message => sent.push(message));
		pane.stamp({ type: 'input_keyboard', eventType: 'keyDown' });
		pane.arrived({ seq: 5 })(true);
		vi.advanceTimersByTime(9_999);
		expect(sent).toEqual([]);
		vi.advanceTimersByTime(1);
		vi.advanceTimersByTime(10_000);
		expect(sent.map(message => message.painted)).toEqual([1, 0]);
		pane.stop();
		vi.advanceTimersByTime(30_000);
		expect(sent).toHaveLength(2);
		expect(pane.stamp({ type: 'input_keyboard', eventType: 'keyDown' })).toMatchObject({ id: 1 });
		expect(pane.report()).toMatchObject({ painted_seq: 0 });
	});
});
