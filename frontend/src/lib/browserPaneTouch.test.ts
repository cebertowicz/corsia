import { afterEach, describe, expect, it, vi } from 'vitest';
import { BrowserPaneTouch, type TextFieldKind } from './browserPaneTouch';

afterEach(() => { document.body.innerHTML = ''; });

describe('touch keyboard target', () => {
 it.each([
  ['email', 'email', 'text', 'email'], ['tel', 'tel', 'text', 'tel'],
  ['number', 'decimal', 'text', 'off'], ['one-time-code', 'numeric', 'text', 'one-time-code'],
  ['password', 'text', 'password', 'current-password'],
 ] satisfies [TextFieldKind, string, string, string][])('sets the %s field attributes', (kind, mode, type, autocomplete) => {
  const wrapper = document.createElement('div');
  const canvas = document.createElement('canvas'); wrapper.append(canvas); document.body.append(wrapper);
  const touch = new BrowserPaneTouch(vi.fn());
  touch.setFields([{ x: 0, y: 0, width: 100, height: 50, kind }]);
  touch.tap({ x: 5, y: 5 }, canvas, { left: 0, top: 0, scale: 1 });
  const input = wrapper.querySelector<HTMLInputElement>('input')!;
  expect(document.activeElement).toBe(input);
  expect([input.inputMode, input.type, input.autocomplete]).toEqual([mode, type, autocomplete]);
  expect(input.spellcheck).toBe(false);
  touch.close();
 });

 it('forwards a completed composition once and clears the target', () => {
  const wrapper = document.createElement('div');
  const canvas = document.createElement('canvas'); wrapper.append(canvas); document.body.append(wrapper);
  const send = vi.fn();
  const touch = new BrowserPaneTouch(send);
  touch.setFields([{ x: 0, y: 0, width: 100, height: 50, kind: 'text' }]);
  touch.tap({ x: 5, y: 5 }, canvas, { left: 0, top: 0, scale: 1 });
  const input = wrapper.querySelector('input')!;
  input.dispatchEvent(new CompositionEvent('compositionstart'));
  input.value = '世界';
  input.dispatchEvent(new InputEvent('input', { isComposing: true, inputType: 'insertCompositionText' }));
  input.dispatchEvent(new CompositionEvent('compositionend'));
  input.value = '世界';
  input.dispatchEvent(new InputEvent('input', { inputType: 'insertFromComposition' }));
  expect(send.mock.calls.map(([message]) => message)).toEqual([{ type: 'clipboard_paste', text: '世界' }]);
  expect(input.value).toBe('');
 });

 it('forwards delete and return, including input-only mobile events', () => {
  const wrapper = document.createElement('div');
  const canvas = document.createElement('canvas'); wrapper.append(canvas); document.body.append(wrapper);
  const send = vi.fn();
  const touch = new BrowserPaneTouch(send);
  touch.setFields([{ x: 0, y: 0, width: 100, height: 50, kind: 'textarea' }]);
  touch.tap({ x: 5, y: 5 }, canvas, { left: 0, top: 0, scale: 1 });
  const input = wrapper.querySelector('textarea')!;
  input.dispatchEvent(new InputEvent('beforeinput', { inputType: 'deleteContentBackward', bubbles: true, cancelable: true }));
  input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
  input.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', bubbles: true, cancelable: true }));
  expect(send.mock.calls.map(([message]) => [message.eventType, message.key, message.keyCode, message.text])).toEqual([
   ['keyDown', 'Backspace', 8, ''], ['keyUp', 'Backspace', 8, ''],
   ['keyDown', 'Enter', 13, '\r'], ['keyUp', 'Enter', 13, ''],
  ]);
 });
});
