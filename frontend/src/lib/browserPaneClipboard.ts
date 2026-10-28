type ClipboardMessage = { type: string; id?: number; text?: string; cut?: boolean };
class EmptySelection extends Error {}
class PageSelectionError extends Error {}
class StreamEnded extends Error {}

/** Name the cause only when it is known; a closed stream already shows its own status. */
function failureMessage(error: unknown, key: string): string | null {
 if (error instanceof StreamEnded) return null;
 if (error instanceof EmptySelection) return 'Select text in the page to copy.';
 if (error instanceof PageSelectionError) return error.message;
 if (error instanceof DOMException && error.name === 'NotAllowedError') return 'CORSIA couldn’t access the clipboard. Allow clipboard access in your browser and try again.';
 return key === 'v' ? 'CORSIA couldn’t paste. Try again.' : 'CORSIA couldn’t copy. Try again.';
}

export function clipboardKey<T>(event: KeyboardEvent, get: () => BrowserPaneClipboard<T>, fail: (error: string, cut: boolean) => void, clear: () => void = () => {}): boolean {
 const key = event.key.toLowerCase();
 const mac = /Mac|iPhone|iPad|iPod/.test(navigator.platform);
 if (event.type !== 'keydown' || !(mac ? event.metaKey : event.ctrlKey) || event.altKey || !['c', 'x', 'v'].includes(key)) return false;
 const clipboard = get();
 void (key === 'v' ? clipboard.paste() : clipboard.copy(key === 'x')).then(() => { if (key !== 'v') clear(); }).catch(error => { const message = failureMessage(error, key); if (message) fail(message, key === 'x' && !(error instanceof EmptySelection)); });
 return true;
}

export function clipboardReply<T>(message: Record<string, unknown>, clipboard: BrowserPaneClipboard<T> | null): boolean {
 if (message.type === 'clipboard_selection' && typeof message.id === 'number') {
  if (typeof message.text === 'string') clipboard?.selection(message.id, message.text);
  else clipboard?.fail(message.id, typeof message.error === 'string' ? message.error : "CORSIA couldn’t copy from the page. Select the text again and copy.");
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
  let selectionFailure: unknown;
  const data = selection.then(text => new Blob([text], { type: 'text/plain' }), error => { selectionFailure = error; throw error; });
  // A disconnected stream can reject after the browser has already refused clipboard access.
  void data.catch(() => {});
  try {
   const writing = this.clipboard.write([this.item({ 'text/plain': data })]);
   this.send({ type: 'clipboard_selection', id, cut });
   try { await writing; } catch (error) { throw selectionFailure ?? error; }
   await selection;
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
  this.pending.get(id)?.reject(new PageSelectionError(error));
  this.pending.delete(id);
 }

 dispose(): void {
  for (const request of this.pending.values()) request.reject(new StreamEnded('Browser stream ended'));
  this.pending.clear();
 }
}
