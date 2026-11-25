import { test } from './support/test';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { createSession, replacePreviousFixtureThread, loadPastFirstRunLanding, openAppByUrl, TEST_TOKEN } from './support/corsia';
import { injectMessage } from './support/mock';
import { setDebugVersionOverlay } from './support/heldComments';

const out = process.env.CORSIA_CAPTURE_DIR ?? path.resolve('test-results/plan-cap-cases');
fs.mkdirSync(out, { recursive: true });
const probe = (v: unknown) => { const line = JSON.stringify(v); console.log(line); fs.appendFileSync(path.join(out, 'probe.jsonl'), line + '\n'); };
const seed = String.raw`
import sys, uuid, asyncio, json, traceback
from datetime import datetime, timedelta, timezone
from infra.database import get_db
from plans import service, plan_cap
from tools.plans import PlanUpdateTool
owner, case = sys.argv[1:]
db=get_db(); p=service.ensure_plan('thread',owner,db=db); db.commit()
tool=PlanUpdateTool()
def emit(kind, value): print(json.dumps({'case':case,'kind':kind,'value':value},default=str),flush=True)
def node(title,kind='task',parent=None,later=False):
 return service.add_node(p.id,kind=kind,title=title,acceptance_criteria='done',parent_id=parent,later=later,db=db).id
def find(title): return db.execute('SELECT id FROM plan_nodes WHERE plan_id=? AND title=?',(p.id,title)).fetchone()['id']
def rev(id): return service.get_node(id,db=db).revision
def rows():
 db.commit(); emit('rows',[dict(r) for r in db.execute('SELECT title,kind,parent_id,later,state,order_key FROM plan_nodes WHERE plan_id=? AND archived_at IS NULL ORDER BY order_key,id',(p.id,)).fetchall()])
async def call(op,params):
 result=await tool.execute(plan_id=p.id,mutation_id=uuid.uuid4().hex,operation=op,params=params,_thread_id=owner)
 emit('tool',{'operation':op,'params':params,'result':result}); return result
async def watch(id,hours):
 await call('arm_watch',{'node_id':id,'expected_revision':rev(id),'condition':{'type':'deadline','value':(datetime.now(timezone.utc)+timedelta(hours=hours)).isoformat()},'reason':'Check back later'})
async def go():
 if case=='A':
  for i in range(1,9): await call('add',{'title':f'Task {i}','intent':f'Complete task {i}','acceptance_criteria':'done','size':'S'})
 elif case=='B':
  id=find('Task 7'); await call('edit',{'node_id':id,'expected_revision':rev(id),'later':False})
 elif case=='C':
  x=node('Check-back X'); y=node('Task Y'); db.commit(); await watch(x,4)
  await call('start',{'node_id':y,'expected_revision':rev(y)})
  await call('reorder',{'node_id':y,'expected_revision':rev(y),'order_key':0})
  await call('start',{'node_id':y,'expected_revision':rev(y)})
 elif case in ('D','E'):
  phase=node('Phase '+('P' if case=='D' else 'Q'),'phase')
  steps=[node(('P' if case=='D' else 'Q')+f' step {i}',parent=phase) for i in range(1,4)]; db.commit()
  await call('edit',{'node_id':steps[2],'expected_revision':rev(steps[2]),'later':True}); rows()
  if case=='E':
   await call('add',{'title':'Q implicit step','intent':'Complete Q implicit step','acceptance_criteria':'done','size':'S','parent_id':phase})
   await call('add',{'title':'Q conflicting step','intent':'Complete Q conflicting step','acceptance_criteria':'done','size':'S','parent_id':phase,'later':False})
 elif case=='D-start':
  id=find('P step 1'); await call('start',{'node_id':id,'expected_revision':rev(id)})
 elif case=='F':
  running=node('Running task'); phase=node('Phase R','phase'); step=node('R check-back',parent=phase); db.commit()
  await call('start',{'node_id':running,'expected_revision':rev(running)}); await watch(step,2)
 elif case=='G':
  sys.path.insert(0, 'tests')
  from doc_feedback_helpers import seed_thread, seed_document, add_person_comment, add_comment, batches
  from doceditor import feedback_batches
  seed_thread(db,'author-'+owner,title='Reviewer'); writing=node('Owner task T'); node('Following task'); db.commit()
  noted=seed_document(db,owner,name='notes-'+owner+'.md'); reviewed=seed_document(db,owner,name='page-'+owner+'.md')
  db.execute('UPDATE artifact_registrations SET origin_plan_node_id=? WHERE artifact_id IN (?,?)',(writing,noted,reviewed)); db.commit()
  add_person_comment(db,noted)
  add_comment(db,reviewed,author_id='author-'+owner)
  batch=next(b for b in batches(db,state='open') if b['artifact_id']==reviewed)
  feedback_batches.close_batch(db,batch['id'],'quiet')
 elif case=='H':
  for i in range(1,9): node(f'H task {i}')
  phase=node('H phase','phase'); steps=[node(f'H step {i}',parent=phase) for i in range(1,4)]
  db.execute('UPDATE plan_nodes SET later=1 WHERE id=?',(steps[0],)); db.commit(); emit('before', [dict(r) for r in db.execute('SELECT title,later FROM plan_nodes WHERE plan_id=? ORDER BY order_key,id',(p.id,)).fetchall()])
  emit('spill_all',plan_cap.spill_all(db))
 rows()
try: asyncio.run(go())
except Exception as e: emit('exception',traceback.format_exc())
`;

function run(owner: string, label: string) {
 const options = {cwd:path.resolve('..','backend'), env:{...process.env,PYTHONPATH:path.resolve('..','backend'),CORSIA_TEST_MODE:'1',CORSIA_WORKSPACE:process.env.CORSIA_E2E_WORKSPACE!,CORSIA_STATE_DIR:process.env.CORSIA_E2E_STATE_DIR!}, encoding:'utf8' as const};
 let text: string;
 try { text=execFileSync(process.env.CORSIA_E2E_BACKEND_PYTHON!, ['-c',seed,owner,label],options); }
 catch(e:any) { probe({case:label,kind:'process-error',value:String(e),stdout:String(e.stdout||''),stderr:String(e.stderr||'')}); return; }
 for(const line of text.trim().split('\n')) { let item: unknown; try { item=JSON.parse(line); } catch { item={case:label,kind:'stdout',value:line}; } probe(item); }
}
async function screenshot(page:any,name:string) {
 await page.mouse.move(0,0); await page.waitForTimeout(400);
 await page.screenshot({path:path.join(out,name+'.png'),animations:'disabled'});
 const v=await page.evaluate((name:string)=>{
  const tree=document.querySelector('.plan-tree')!; const rows=[...tree.querySelectorAll('.tree-row')];
  const visible=rows.filter(e=>{const r=e.getBoundingClientRect();const hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);return !!hit&&e.contains(hit)});
  const title=(e:Element)=>(e.querySelector('.row-title') as HTMLElement|null)?.innerText.trim()||'';
  return {name,visibleTitles:visible.map(title).filter(Boolean),visibleRowText:visible.map(e=>(e as HTMLElement).innerText.trim()),expanded:[...tree.querySelectorAll('[aria-expanded="true"]')].map(e=>(e.closest('.tree-row') as HTMLElement|null)?.innerText.trim()|| (e as HTMLElement).innerText.trim()),laterText:(tree.querySelector('.scheduled-row') as HTMLElement|null)?.innerText.trim()||null};
 },name); probe({kind:'frame',...v});
}
async function view(page:any,request:any,owner:string,title:string,width:number) {
 await page.setViewportSize({width,height:width===393?852:900}); await page.emulateMedia({colorScheme:'light'}); await setDebugVersionOverlay(request,false);
 await injectMessage(request,owner,{role:'user',content:'Please work on this plan.'},TEST_TOKEN);
 await loadPastFirstRunLanding(page,()=>openAppByUrl(page),{lands:true});
 if(width===393) await page.getByRole('button',{name:'Toggle sidebar'}).click();
 await page.locator('.thread-item').filter({hasText:title}).first().click();
 if(width===393) await page.getByRole('button',{name:/Open plan/}).click();
 await page.locator('.plan-tree').waitFor();
}
async function attempt(label:string, fn:()=>Promise<void>) { try { await fn(); } catch(e) { probe({kind:'frame-error',name:label,error:String(e)}); } }
test.describe.configure({mode:'serial'});
test('cases A and B',async({page,request})=>{
 test.setTimeout(240000); const title='Case A plan'; const owner=await createSession(request,title); await replacePreviousFixtureThread(request,owner); run(owner,'A');
 await attempt('A-1440',async()=>{await view(page,request,owner,title,1440);await page.locator('.plan-tree').getByText('Task 1',{exact:true}).waitFor();await screenshot(page,'A-1440');await page.locator('.scheduled-row button').first().click();await page.locator('.plan-tree').getByText('Task 6',{exact:true}).waitFor();await screenshot(page,'A-later-open-1440');});
 await attempt('A-393',async()=>{await view(page,request,owner,title,393);await screenshot(page,'A-393');}); run(owner,'B');
});
for(const label of ['C','D','E','F','G','H']) test('case '+label,async({page,request})=>{
 test.setTimeout(240000);const title='Case '+label+' plan';const owner=await createSession(request,title);await replacePreviousFixtureThread(request,owner);run(owner,label);
 if(['C','D','F','G'].includes(label)) await attempt(label+'-1440',async()=>{await view(page,request,owner,title,1440);if(label==='D') { await page.locator('.scheduled-row button').first().click(); await page.locator('.plan-tree').getByText('Phase P',{exact:true}).waitFor(); } await screenshot(page,label+'-1440');});
 if(label==='F') await attempt('F-393',async()=>{await view(page,request,owner,title,393);await screenshot(page,'F-393');});
 if(label==='D') run(owner,'D-start');
});
