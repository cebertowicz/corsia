type ErrorSource = 'fit' | 'clipboard';

/** Keep each source until that source succeeds; the newest error is visible. */
export class BrowserPaneErrors {
 private errors = $state<{ source: ErrorSource; error: string }[]>([]);
 current = $derived(this.errors.at(-1)?.error ?? '');
 private cutFailed = $state(false);
 canUndoCut = $derived(this.cutFailed && this.errors.at(-1)?.source === 'clipboard');

 handle(message: Record<string, unknown>): boolean {
  if (message.type !== 'pane_error' || (message.source !== 'fit' && message.source !== 'clipboard')
      || (message.error !== null && typeof message.error !== 'string')) return false;
  this.set(message.source, message.error);
  return true;
 }

 resetUndo(): void { this.cutFailed = false; }

 set(source: ErrorSource, error: string | null, cut = false): void {
  if (source === 'clipboard') this.cutFailed = !!error && cut;
  this.errors = this.errors.filter(item => item.source !== source);
  if (error) this.errors = [...this.errors, { source, error }];
 }
}
