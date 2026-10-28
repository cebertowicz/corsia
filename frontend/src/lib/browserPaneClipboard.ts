type ClipboardMessage = { type: string; id?: number; text?: string };
class EmptySelection extends Error {}

export function clipboardKey<T>(event: KeyboardEvent, keys: Set<string>, get: () => BrowserPaneClipboard<T>, fail: () => void): boolean {
 if (event.type === 'keyup' && keys.delete(event.code)) return true;
 const key = event.key.toLowerCase();
 if (event.type !== 'keydown' || !(event.metaKey || event.ctrlKey) || event.altKey || !['c', 'x', 'v'].includes(key)) return false;
 keys.add(event.code);
 const clipboard = get();
 void (key === 'v' ? clipboard.paste() : clipboard.copy(key === 'x')).catch(error => { if (!(error instanceof EmptySelection)) fail(); });
 return true;
}

export function clipboardReply<T>(message: Record<string, unknown>, clipboard: BrowserPaneClipboard<T> | null, fail: (error: string) => void): boolean {
 if (message.type === 'clipboard_selection' && typeof message.id === 'number') {
  if (typeof message.text === 'string') clipboard?.selection(message.id, message.text);
  else clipboard?.fail(message.id, typeof message.error === 'string' ? message.error : 'Could not copy from the page');
  return true;
 }
 if (message.type === 'clipboard_error' && typeof message.error === 'string') {
  fail(message.error);
  return true;
 }
 return false;
}
type ClipboardAccess<T> = { write: (items: T[]) => Promise<void>; readText: () => Promise<string> };

/** Bridge the person's clipboard only in response to their pane input. */
export class BrowserPaneClipboard<T> {
 private nextId = 0;
 private pending = new Map<number, { resolve: (text: string) => void; reject: (error: Error) => void }>();
 constructor(private send: (message: ClipboardMessage) => void, private clipboard: ClipboardAccess<T>,
             private item: (data: Record<string, Promise<Blob>>) => T) {}

 async copy(cut: boolean): Promise<void> {
  const id = ++this.nextId;
  const selection = new Promise<string>((resolve, reject) => this.pending.set(id, { resolve, reject }));
  // The browser sees clipboard.write during the user's key gesture, even though the page selection
  // arrives asynchronously. ClipboardItem accepts the promised bytes for precisely that purpose.
  const data = selection.then(text => new Blob([text], { type: 'text/plain' }));
  // A disconnected stream can reject after the browser has already refused clipboard access.
  void data.catch(() => {});
  try {
   const writing = this.clipboard.write([this.item({ 'text/plain': data })]);
   this.send({ type: 'clipboard_selection', id });
   await writing;
   const text = await selection;
   if (cut && text) this.send({ type: 'clipboard_cut', text });
  } finally {
   this.pending.delete(id);
  }
 }

 async paste(): Promise<void> {
  const text = await this.clipboard.readText();
  this.send({ type: 'clipboard_paste', text });
 }

 selection(id: number, text: string): void {
  const request = this.pending.get(id);
  if (text) request?.resolve(text);
  else request?.reject(new EmptySelection());
  this.pending.delete(id);
 }

 fail(id: number, error: string): void {
  this.pending.get(id)?.reject(new Error(error));
  this.pending.delete(id);
 }

 dispose(): void {
  for (const request of this.pending.values()) request.reject(new Error('Browser stream ended'));
  this.pending.clear();
 }
}
