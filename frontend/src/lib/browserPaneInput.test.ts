import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { BrowserPaneInput, wheelPixels } from './browserPaneInput';

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
 it('sums touch wheel deltas per display frame and preserves their order before release', () => {
  const send = vi.fn(); const shifts = vi.fn(); const input = new BrowserPaneInput(send, shifts);
  input.press(pointer(), { x: 40, y: 400 });
  input.move(pointer('touch', 170), { x: 40, y: 340 }, .5);
  input.move(pointer('touch', 130), { x: 40, y: 260 }, .5);
  expect(send).not.toHaveBeenCalled();
  expect(shifts.mock.calls).toEqual([[{ x: 0, y: 60 }], [{ x: 0, y: 80 }]]);
  input.move(pointer('touch', 110), { x: 40, y: 220 }, .5);
  input.release(pointer('touch', 110), { x: 40, y: 220 });
  expect(send.mock.calls.map(([message]) => message)).toEqual([
   { type: 'input_mouse', eventType: 'mouseWheel', x: 40, y: 220, deltaX: 0, deltaY: 180 },
  ]);
  paint(); expect(send).toHaveBeenCalledTimes(1);
 });

 it('flushes coalesced wheel input before a discrete mouse press', () => {
  const send = vi.fn(); const input = new BrowserPaneInput(send);
  input.wheel({ x: 20, y: 30 }, { deltaX: 0, deltaY: 10 });
  input.wheel({ x: 30, y: 40 }, { deltaX: 0, deltaY: 20 });
  input.press(pointer('mouse'), { x: 50, y: 60 });
  expect(send.mock.calls.map(([message]) => message)).toEqual([
   { type: 'input_mouse', eventType: 'mouseWheel', x: 30, y: 40, deltaX: 0, deltaY: 30 },
   { type: 'input_mouse', eventType: 'mouseMoved', x: 50, y: 60, button: 'none', clickCount: 0 },
   { type: 'input_mouse', eventType: 'mousePressed', x: 50, y: 60, button: 'left', clickCount: 1 }
  ]);
 });
 it('resets a held pointer so the next press is delivered', () => {
  const send = vi.fn(); const input = new BrowserPaneInput(send);
  input.press(pointer('mouse'), { x: 10, y: 20 });
  input.reset();
  input.press({ ...pointer('mouse'), pointerId: 2 }, { x: 30, y: 40 });
  expect(send.mock.calls.map(([message]) => message.eventType)).toEqual(['mouseMoved','mousePressed','mouseMoved','mousePressed']);
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

describe('continuous input uses one display-frame owner', () => {
 it('combines wheel deltas with the latest pointer position into one event', () => {
  const send = vi.fn(); const input = new BrowserPaneInput(send);
  input.move(pointer('mouse'), {x: 1, y: 2}, 1);
  input.wheel({x: 3, y: 4}, {deltaX: 2, deltaY: 20});
  input.move(pointer('mouse'), {x: 5, y: 6}, 1);
  input.wheel({x: 7, y: 8}, {deltaX: 3, deltaY: -10});
  expect(pending.size).toBe(1);
  paint();
  expect(send.mock.calls.map(([message]) => message)).toEqual([
   {type:'input_mouse',eventType:'mouseWheel',x:7,y:8,deltaX:5,deltaY:10}
  ]);
  input.move(pointer('mouse'), {x:9,y:10},1);
  input.flush();
  send({type:'input_keyboard',eventType:'keyDown',key:'a'});
  expect(send.mock.calls.slice(-2).map(([message])=>message.eventType)).toEqual(['mouseMoved','keyDown']);
  paint();
  expect(send).toHaveBeenCalledTimes(3);
 });
});

describe('wheelPixels', () => {
 it('keeps pixel deltas and converts line and page deltas to CSS pixels', () => {
  expect(wheelPixels({ deltaX: 3, deltaY: -90, deltaMode: 0 }, 600)).toEqual({ deltaX: 3, deltaY: -90 });
  expect(wheelPixels({ deltaX: 1, deltaY: 3, deltaMode: 1 }, 600)).toEqual({ deltaX: 40, deltaY: 120 });
  expect(wheelPixels({ deltaX: 0, deltaY: -1, deltaMode: 2 }, 600)).toEqual({ deltaX: 0, deltaY: -600 });
 });
});
