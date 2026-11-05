import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import MentionPickerHarness from './MentionPickerHarness.svelte';
import { renderMentions } from './mentionMarkup';
import { createMentionSelection } from './mentionSelection';
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
 it('offers the taggable roster including CORSIA and the document owner, and retries roster errors',()=>{
  const retry=vi.fn(async()=>{}); const all=[agent('corsia','CORSIA'), agent('owner','Document Owner')];
  host=document.createElement('div'); document.body.append(host); component=mount(MentionPickerHarness,{target:host,props:{agents:all}}); flushSync();
  const field=host.querySelector('textarea')!; field.value='@'; field.setSelectionRange(1,1); field.dispatchEvent(new Event('input',{bubbles:true})); flushSync(); expect(host.querySelectorAll('[role=option]')).toHaveLength(2);
  unmount(component); component=undefined; host.replaceChildren(); component=mount(MentionPickerHarness,{target:host,props:{agents:all,status:'error',retry}}); flushSync();
  const next=host.querySelector('textarea')!; next.value='@'; next.setSelectionRange(1,1); next.dispatchEvent(new Event('input',{bubbles:true})); flushSync(); host.querySelector('button')!.click(); expect(retry).toHaveBeenCalledOnce();
 });
 it('keeps the selected identity when the picker is destroyed and recreated with its draft',()=>{
  const selectedTags=createMentionSelection(); host=document.createElement('div'); document.body.append(host);
  component=mount(MentionPickerHarness,{target:host,props:{agents,selectedTags}}); flushSync();
  const field=host.querySelector('textarea')!; field.value='@'; field.setSelectionRange(1,1); field.dispatchEvent(new Event('input',{bubbles:true})); flushSync();
  field.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true})); flushSync();
  const body=field.value; expect((component as {taggedAgentIds:()=>string[]}).taggedAgentIds()).toEqual(['a']);
  unmount(component); component=mount(MentionPickerHarness,{target:host,props:{agents,selectedTags,body}}); flushSync();
  expect((component as {taggedAgentIds:()=>string[]}).taggedAgentIds()).toEqual(['a']);
 });
 it('colors only the selected occurrence when names and typed text are identical',()=>{
  const el=document.createElement('div');
  el.innerHTML=renderMentions('😀 @Same and @Same', [{id:'a',name:'Same'},{id:'b',name:'Same'}], [{agent_id:'b',start:13,end:18}]);
  expect(el.querySelectorAll('.comment-mention')).toHaveLength(2);
  expect(el.querySelector('p')?.textContent).toBe('😀 @Same and @Same');
  expect(el.querySelector('p .comment-mention')?.getAttribute('data-agent-id')).toBe('b');
 });
 it('shows a tool-selected tag separately without coloring typed text',()=>{
  const el=document.createElement('div'); el.innerHTML=renderMentions('@Alpha Agent please review', [agents[0]]);
  expect(el.querySelector('p')?.textContent).toBe('@Alpha Agent please review');
  expect(el.querySelector('.comment-mention')?.textContent).toBe('@Alpha Agent');
 });
});
