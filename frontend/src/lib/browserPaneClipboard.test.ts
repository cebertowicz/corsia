import { describe, expect, it, vi } from 'vitest';
import { BrowserPaneClipboard, clipboardKey, clipboardReply } from './browserPaneClipboard';

describe('browser pane clipboard', () => {
 it('reports a page selection failure with its next step', async () => {
  const clipboard = new BrowserPaneClipboard(vi.fn(), { write: vi.fn(async (items: Record<string, Promise<Blob>>[]) => { await items[0]['text/plain']; }), readText: vi.fn() }, data => data);
  const fail = vi.fn();
  clipboardKey({ type: 'keydown', key: 'c', code: 'KeyC', ctrlKey: true, metaKey: false, altKey: false } as KeyboardEvent, new Set(), () => clipboard, fail);
  clipboardReply({ type: 'clipboard_selection', id: 1 }, clipboard, fail);
  await vi.waitFor(() => expect(fail).toHaveBeenCalledWith("CORSIA couldn’t copy from the page. Select the text again and copy."));
 });
 it('names clipboard permission only when the browser refused it', async () => {
  const keydown = { type: 'keydown', key: 'c', code: 'KeyC', ctrlKey: true, metaKey: false, altKey: false } as KeyboardEvent;
  const refused = new BrowserPaneClipboard(vi.fn(), { write: vi.fn().mockRejectedValue(new DOMException('denied', 'NotAllowedError')), readText: vi.fn() }, data => data);
  const broken = new BrowserPaneClipboard(vi.fn(), { write: vi.fn().mockRejectedValue(new Error('write failed')), readText: vi.fn() }, data => data);
  const fail = vi.fn();
  clipboardKey(keydown, new Set(), () => refused, fail);
  clipboardKey(keydown, new Set(), () => broken, fail);
  await vi.waitFor(() => expect(fail).toHaveBeenCalledTimes(2));
  expect(fail.mock.calls.map(call => call[0]).sort()).toEqual([
   'CORSIA couldn’t access the clipboard. Allow clipboard access in your browser and try again.',
   'CORSIA couldn’t copy. Try again.',
  ]);
 });
 it('stays quiet when the stream ends during a copy', async () => {
  const clipboard = new BrowserPaneClipboard(vi.fn(), { write: vi.fn(async (items: Record<string, Promise<Blob>>[]) => { await items[0]['text/plain']; }), readText: vi.fn() }, data => data);
  const fail = vi.fn();
  clipboardKey({ type: 'keydown', key: 'c', code: 'KeyC', ctrlKey: true, metaKey: false, altKey: false } as KeyboardEvent, new Set(), () => clipboard, fail);
  clipboard.dispose();
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(fail).not.toHaveBeenCalled();
 });
 it('reports selection errors even when Chromium rejects the promised clipboard data', async () => {
  const clipboard = new BrowserPaneClipboard(vi.fn(), { write: vi.fn(async (items: Record<string, Promise<Blob>>[]) => {
   try { await items[0]['text/plain']; } catch { throw new Error('clipboard write rejected'); }
  }), readText: vi.fn() }, data => data);
  const result = clipboard.copy(false);
  clipboard.fail(1, 'Selection failed');
  await expect(result).rejects.toThrow('Selection failed');
 });
 it('starts clipboard write during copy, then supplies the page selection', async () => {
  const send = vi.fn();
  let item: Record<string, Promise<Blob>> = {};
  const write = vi.fn(async (items: Record<string, Promise<Blob>>[]) => { item = items[0]; await item['text/plain']; });
  const clipboard = new BrowserPaneClipboard(send, { write, readText: vi.fn() }, function (data) { return data; });
  const copied = clipboard.copy(false);
  expect(write).toHaveBeenCalledOnce();
  expect(send).toHaveBeenCalledWith({ type: 'clipboard_selection', id: 1 });
  clipboard.selection(1, 'selected');
  await copied;
  expect(await (await item['text/plain']).text()).toBe('selected');
  expect(send).toHaveBeenCalledTimes(1);
 });
 it('deletes selected text only after a successful cut write', async () => {
  const send = vi.fn();
  const write = vi.fn(async (items: Record<string, Promise<Blob>>[]) => { await items[0]['text/plain']; });
  const clipboard = new BrowserPaneClipboard(send, { write, readText: vi.fn() }, function (data) { return data; });
  const cut = clipboard.copy(true);
  clipboard.selection(1, 'selected');
  await cut;
  expect(send).toHaveBeenLastCalledWith({ type: 'clipboard_cut', id: 1, text: 'selected' });
 });
 it('reads the host clipboard on paste and sends text to the page', async () => {
  const send = vi.fn();
  const readText = vi.fn().mockResolvedValue('pasted');
  const clipboard = new BrowserPaneClipboard(send, { write: vi.fn(), readText }, function (data) { return data; });
  await clipboard.paste();
  expect(readText).toHaveBeenCalledOnce();
  expect(send).toHaveBeenCalledWith({ type: 'clipboard_paste', text: 'pasted' });
 });
 it('does not replace the clipboard when the page has no selection', async () => {
  const send = vi.fn();
  const write = vi.fn(async (items: Record<string, Promise<Blob>>[]) => { await items[0]['text/plain']; });
  const clipboard = new BrowserPaneClipboard(send, { write, readText: vi.fn() }, function (data) { return data; });
  const copy = clipboard.copy(false);
  clipboard.selection(1, '');
  await expect(copy).rejects.toThrow();
 });
 it('reports clipboard refusal even if the page never replies', async () => {
  const clipboard = new BrowserPaneClipboard(vi.fn(), { write: vi.fn().mockRejectedValue(new Error('denied')), readText: vi.fn() }, data => data);
  await expect(clipboard.copy(false)).rejects.toThrow('denied');
 });
 it('does not delete on clipboard write refusal', async () => {
  const send = vi.fn();
  const clipboard = new BrowserPaneClipboard(send, { write: vi.fn().mockRejectedValue(new Error('denied')), readText: vi.fn() }, function (data) { return data; });
  const cut = clipboard.copy(true);
  clipboard.selection(1, 'selected');
  await expect(cut).rejects.toThrow('denied');
  expect(send).not.toHaveBeenCalledWith({ type: 'clipboard_cut', id: 1, text: 'selected' });
 });
});
