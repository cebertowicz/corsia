import { keyMessage } from './browserPaneInput';
import type { PanePoint } from './browserPane';

export type TextFieldKind = 'text' | 'email' | 'number' | 'tel' | 'url' | 'search' | 'password' | 'one-time-code' | 'textarea' | 'editable';
export type TextField = PanePoint & { width: number; height: number; kind: TextFieldKind };

const kinds = new Set<TextFieldKind>(['text', 'email', 'number', 'tel', 'url', 'search', 'password', 'one-time-code', 'textarea', 'editable']);

/** A keyboard target for the streamed page. It keeps no text after forwarding an edit. */
export class BrowserPaneTouch {
 private fields: TextField[] = [];
 private input: HTMLInputElement | HTMLTextAreaElement | null = null;

 constructor(private readonly send: (message: object) => void, private readonly pointer: (event: PointerEvent) => void = () => {}) {}

 setFields(value: unknown): void {
  if (!Array.isArray(value)) return;
  this.fields = value.filter((field): field is TextField => field && typeof field === 'object' &&
   ['x', 'y', 'width', 'height'].every(key => typeof field[key] === 'number' && Number.isFinite(field[key])) &&
   field.width > 0 && field.height > 0 && kinds.has(field.kind));
 }

 clear(): void { this.fields = []; this.close(); }
 close(): void { this.input?.blur(); this.input?.remove(); this.input = null; }

 tap(point: PanePoint, canvas: HTMLCanvasElement, placement: {left: number; top: number; scale: number}): void {
  const field = this.fields.find(item => point.x >= item.x && point.x < item.x + item.width && point.y >= item.y && point.y < item.y + item.height);
  this.close();
  if (!field || !canvas.parentElement) return;
  const input = document.createElement(field.kind === 'textarea' || field.kind === 'editable' ? 'textarea' : 'input');
  if (input instanceof HTMLInputElement) input.type = field.kind === 'password' ? 'password' : 'text';
  input.dataset.paneField = '';
  input.inputMode = field.kind === 'one-time-code' ? 'numeric' : ['email', 'number', 'tel', 'url', 'search'].includes(field.kind) ? (field.kind === 'number' ? 'decimal' : field.kind) as HTMLInputElement['inputMode'] : 'text';
  input.autocomplete = field.kind === 'one-time-code' ? 'one-time-code' : field.kind === 'password' ? 'current-password' : ['email', 'tel', 'url'].includes(field.kind) ? field.kind as AutoFill : 'off';
  input.autocapitalize = 'off';
  input.setAttribute('autocorrect', 'off');
  input.spellcheck = false;
  Object.assign(input.style, { position: 'absolute', left: `${canvas.offsetLeft + placement.left + field.x * placement.scale}px`, top: `${canvas.offsetTop + placement.top + field.y * placement.scale}px`, width: `${field.width * placement.scale}px`, height: `${field.height * placement.scale}px`, opacity: '0', color: 'transparent', caretColor: 'transparent', fontSize: 'var(--text-input, 16px)'  });
  const forward = () => {
   const text = input.value;
   input.value = '';
   if (!text) return;
   if (text.length === 1) {
    this.key(text, 'keydown');
    this.key(text, 'keyup');
   } else this.send({ type: 'clipboard_paste', text });
  };
  let committedComposition = false;
  input.addEventListener('compositionstart', () => { committedComposition = false; });
  input.addEventListener('input', event => {
   const edit = event as InputEvent;
   if (edit.isComposing) return;
   if (committedComposition && ['insertFromComposition', 'insertCompositionText'].includes(edit.inputType)) input.value = '';
   else forward();
   committedComposition = false;
  });
  input.addEventListener('compositionend', () => { forward(); committedComposition = true; });
  input.addEventListener('keydown', event => {
   const keyboard = event as KeyboardEvent;
   if (keyboard.key !== 'Backspace' && keyboard.key !== 'Enter') return;
   event.preventDefault();
   this.key(keyboard.key, 'keydown');
  });
  input.addEventListener('keyup', event => {
   const keyboard = event as KeyboardEvent;
   if (keyboard.key === 'Backspace' || keyboard.key === 'Enter') { event.preventDefault(); this.key(keyboard.key, 'keyup'); }
  });
  input.addEventListener('beforeinput', event => {
   const kind = (event as InputEvent).inputType;
   if (kind === 'deleteContentBackward' || kind === 'insertLineBreak' || kind === 'insertParagraph') {
    event.preventDefault();
    const key = kind === 'deleteContentBackward' ? 'Backspace' : 'Enter';
    this.key(key, 'keydown'); this.key(key, 'keyup');
   }
  });
  input.addEventListener('blur', () => { input.value = ''; });
  input.addEventListener('pointerdown', event => {
   const pointer = event as PointerEvent;
   if (pointer.pointerType !== 'touch' || !pointer.isPrimary) return;
   this.pointer(pointer);
   const move = (next: PointerEvent) => { if (next.pointerId === pointer.pointerId) this.pointer(next); };
   const end = (next: PointerEvent) => {
    if (next.pointerId !== pointer.pointerId) return;
    window.removeEventListener('pointermove', move, true);
    window.removeEventListener('pointerup', end, true);
    window.removeEventListener('pointercancel', end, true);
    this.pointer(next);
   };
   window.addEventListener('pointermove', move, true);
   window.addEventListener('pointerup', end, true);
   window.addEventListener('pointercancel', end, true);
  });
  canvas.parentElement.appendChild(input);
  this.input = input;
  input.focus({ preventScroll: true });
 }

 private key(key: string, type: 'keydown' | 'keyup'): void {
  const keyCode = key === 'Backspace' ? 8 : key === 'Enter' ? 13 : 0;
  this.send(keyMessage(new KeyboardEvent(type, { key, code: keyCode ? key : '', keyCode })));
 }
}
