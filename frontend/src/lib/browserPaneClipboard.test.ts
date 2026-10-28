import { describe, expect, it, vi } from 'vitest';
import { BrowserPaneClipboard } from './browserPaneClipboard';

describe('browser pane clipboard', () => {
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
  expect(send).toHaveBeenLastCalledWith({ type: 'clipboard_cut', text: 'selected' });
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
 it('does not delete on clipboard write refusal', async () => {
  const send = vi.fn();
  const clipboard = new BrowserPaneClipboard(send, { write: vi.fn().mockRejectedValue(new Error('denied')), readText: vi.fn() }, function (data) { return data; });
  const cut = clipboard.copy(true);
  clipboard.selection(1, 'selected');
  await expect(cut).rejects.toThrow('denied');
  expect(send).not.toHaveBeenCalledWith({ type: 'clipboard_cut', text: 'selected' });
 });
});
