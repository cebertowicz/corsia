import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { BrowserPaneInput } from './browserPaneInput';

let pending: Map<number, FrameRequestCallback>;
let id: number;
beforeEach(() => {
 pending = new Map(); id = 0;
 vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { pending.set(++id, callback); return id; });
 vi.stubGlobal('cancelAnimationFrame', (key: number) => pending.delete(key));
});
afterEach(() => vi.unstubAllGlobals());
function paint() { const callbacks = [...pending.values()]; pending.clear(); callbacks.forEach(callback => callback(0)); }
function pointer(pointerType = 'touch', clientY = 200) { return { pointerId: 1, pointerType, clientX: 20, clientY }; }

describe('pane pointer input', () => {
 it('moves to a mouse press and preserves all down/up events while coalescing moves', () => {
  const send = vi.fn(); const input = new BrowserPaneInput(send);
  input.press(pointer('mouse'), { x: 10, y: 20 });
  input.move(pointer('mouse'), { x: 30, y: 40 }, 1);
  input.move(pointer('mouse'), { x: 50, y: 60 }, 1);
  paint();
  input.release(pointer('mouse'), { x: 100, y: 80 });
  expect(send.mock.calls.map(([message]) => message.eventType)).toEqual(['mouseMoved','mousePressed','mouseMoved','mouseReleased']);
  expect(send.mock.calls[2][0]).toMatchObject({ x: 50, y: 60, button: 'left' });
  expect(send.mock.calls[3][0]).toMatchObject({ x: 100, y: 80 });
 });
 it('accumulates touch scroll distances in page pixels with flat wheel coordinates', () => {
  const send = vi.fn(); const input = new BrowserPaneInput(send);
  input.press(pointer(), { x: 40, y: 400 });
  input.move(pointer('touch', 170), { x: 40, y: 340 }, .5);
  input.move(pointer('touch', 130), { x: 40, y: 260 }, .5);
  expect(send).not.toHaveBeenCalled();
  paint();
  expect(send).toHaveBeenCalledExactlyOnceWith({ type: 'input_mouse', eventType: 'mouseWheel', x: 40, y: 260, deltaX: 0, deltaY: 140 });
  input.move(pointer('touch', 110), { x: 40, y: 220 }, .5);
  input.release(pointer('touch', 110), { x: 40, y: 220 });
  expect(send.mock.calls[1][0]).toMatchObject({ eventType: 'mouseWheel', deltaY: 40 });
  paint(); expect(send).toHaveBeenCalledTimes(2);
 });
 it('turns a stationary touch into a click and cancellation into no click', () => {
  const send = vi.fn(); const input = new BrowserPaneInput(send);
  input.press(pointer(), { x: 20, y: 200 });
  input.move(pointer('touch', 198), { x: 20, y: 198 }, 1);
  input.release(pointer('touch', 198), { x: 20, y: 198 });
  expect(send.mock.calls.map(([message]) => message.eventType)).toEqual(['mouseMoved','mousePressed','mouseReleased']);
  send.mockClear(); input.press(pointer(), { x: 20, y: 200 });
  input.release(pointer(), { x: 20, y: 200 }, true);
  expect(send).not.toHaveBeenCalled();
 });
});
