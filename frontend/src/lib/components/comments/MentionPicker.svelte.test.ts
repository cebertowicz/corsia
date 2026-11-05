import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import MentionPickerHarness from './MentionPickerHarness.svelte';
import { colorMentions } from './mentionMarkup';
import optionsSource from '../reviews/AgentOptions.svelte?raw';
const agent = (id: string, name: string) => ({ id, name, responsibility:'', requested:false, unavailable_reason:null, unavailable_text:null });
const agents = [agent('a','Alpha Agent'), agent('b','Beta Agent')];
let host: HTMLElement; let component: Record<string, unknown> | undefined;
afterEach(() => { if(component) unmount(component); component = undefined; host?.remove(); });
describe('comment mentions', () => {
 it('allows option names to wrap without clipping', () => {
  const rule = optionsSource.match(/\.option-name\s*\{([^}]*)\}/)?.[1];
  expect(rule).toBeDefined(); expect(rule).toContain('overflow-wrap: anywhere');
  expect(rule).not.toMatch(/line-clamp|overflow\s*:\s*hidden|text-overflow/);
 });
 it('filters, navigates, inserts, and closes with Escape', async () => {
  HTMLElement.prototype.scrollIntoView = vi.fn();
  host=document.createElement('div');document.body.append(host);component=mount(MentionPickerHarness,{target:host,props:{agents}});flushSync();
  const field=host.querySelector('textarea')!; field.value='Hello @';field.setSelectionRange(7,7);field.dispatchEvent(new Event('input',{bubbles:true}));flushSync();
  expect(host.querySelectorAll('[role=option]')).toHaveLength(2);
  expect(field.getAttribute('aria-autocomplete')).toBe('list'); expect(field.getAttribute('aria-controls')).toBe(host.querySelector('[role=listbox]')?.id); expect(field.getAttribute('aria-activedescendant')).toBe(host.querySelector('[aria-selected=true]')?.id); expect(field.hasAttribute('role')).toBe(false);
  field.dispatchEvent(new KeyboardEvent('keydown',{key:'End',bubbles:true,cancelable:true}));flushSync();expect(host.querySelector('[aria-selected=true]')?.textContent).toContain('Beta Agent'); expect(field.getAttribute('aria-activedescendant')).toBe(host.querySelector('[aria-selected=true]')?.id);
  field.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}));flushSync();expect(field.value).toBe('Hello @Beta Agent ');
  expect((component as { taggedAgentIds: () => string[] }).taggedAgentIds()).toEqual(['b']);
  field.value='Hello @Beta Agent next';field.setSelectionRange(field.value.length,field.value.length);field.dispatchEvent(new Event('input',{bubbles:true}));flushSync();await vi.waitFor(() => expect(host.querySelector('[role=listbox]')).toBeNull());
  field.value='@Al';field.setSelectionRange(3,3);field.dispatchEvent(new Event('input',{bubbles:true}));flushSync();expect(host.querySelectorAll('[role=option]')).toHaveLength(1);
  field.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}));flushSync();expect(host.querySelector<HTMLElement>('.mention-region')?.inert).toBe(true); await vi.waitFor(() => expect(host.querySelector('[role=listbox]')).toBeNull()); expect(field.hasAttribute('aria-autocomplete')).toBe(false); expect(field.hasAttribute('aria-controls')).toBe(false); expect(field.hasAttribute('aria-activedescendant')).toBe(false);
 });
 it.each([
  { state:'loading' as const, agents, copy:'Loading agents…' },
  { state:'error' as const, agents, copy:"Couldn't load agents." },
  { state:'ready' as const, agents:[], copy:'No agents in this project to tag.' },
  { state:'ready' as const, agents, rosterError:true, copy:"Couldn't load agents." },
 ])('shows the correct roster state: $copy', ({state, agents, rosterError, copy}) => {
  host=document.createElement('div'); document.body.append(host); component=mount(MentionPickerHarness,{target:host,props:{agents,status:state,rosterError}}); flushSync();
  const field=host.querySelector('textarea')!; field.value='@'; field.setSelectionRange(1,1); field.dispatchEvent(new Event('input',{bubbles:true})); flushSync();
  expect(host.querySelector('[role=listbox]')?.textContent).toContain(copy); expect(host.textContent).not.toContain('No matching agents.');
  expect(field.hasAttribute('aria-activedescendant')).toBe(false);
 });
 it('offers requested and unavailable agents and retries roster errors',()=>{
  const retry=vi.fn(async()=>{}); const all=[{...agents[0],requested:true},{...agents[1],unavailable_reason:'busy'}];
  host=document.createElement('div'); document.body.append(host); component=mount(MentionPickerHarness,{target:host,props:{agents:all}}); flushSync();
  const field=host.querySelector('textarea')!; field.value='@'; field.setSelectionRange(1,1); field.dispatchEvent(new Event('input',{bubbles:true})); flushSync(); expect(host.querySelectorAll('[role=option]')).toHaveLength(2);
  unmount(component); component=undefined; host.replaceChildren(); component=mount(MentionPickerHarness,{target:host,props:{agents:all,status:'error',retry}}); flushSync();
  const next=host.querySelector('textarea')!; next.value='@'; next.setSelectionRange(1,1); next.dispatchEvent(new Event('input',{bubbles:true})); flushSync(); host.querySelector('button')!.click(); expect(retry).toHaveBeenCalledOnce();
 });
 it('decorates agents already requested for review',()=>{
  const el=document.createElement('div');el.innerHTML=colorMentions('<p>@Alpha Agent</p>',[agents[0]]);
  expect(el.querySelector('.comment-mention')?.textContent).toBe('@Alpha Agent');
 });
 it('adds a tool-selected tag without duplicating an inline tag',()=>{
  const el=document.createElement('div'); el.innerHTML=colorMentions('<p>@Alpha Agent please review</p>',agents);
  expect(el.textContent).toBe('@Alpha Agent please review @Beta Agent');
  expect(el.querySelectorAll('.comment-mention')).toHaveLength(2);
 });
 it('colors matched names and leaves unmatched mentions plain',()=>{
  const el=document.createElement('div');el.innerHTML=colorMentions('<p>Hello @Alpha Agent and @Unknown.</p>',[agents[0]]);
  expect(el.querySelector('.comment-mention')?.textContent).toBe('@Alpha Agent');expect(el.textContent).toBe('Hello @Alpha Agent and @Unknown.');expect(el.querySelectorAll('.comment-mention')).toHaveLength(1);
 });
});
