
let models=[],selModel=null,curRunId=null,evtSource=null;

// ── 折叠面板 ──
function toggleSection(head){
  head.classList.toggle('open');
  const body=head.nextElementSibling;
  body.classList.toggle('open');
  if(body.classList.contains('open') && body.id==='secKeys') loadKeys();
  if(body.classList.contains('open') && body.id==='secConfig') loadConfig();
}
function toggleAll(open){document.querySelectorAll('.section-head').forEach(h=>{if(open)h.classList.add('open');else h.classList.remove('open');const b=h.nextElementSibling;if(open)b.classList.add('open');else b.classList.remove('open')})}

// ── tabs ──
function switchTab(name,btn){
  document.querySelectorAll('.tab-panel').forEach(p=>p.classList.remove('active'));
  document.getElementById('tab-'+name).classList.add('active');
  document.querySelectorAll('header .nav button').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  if(name==='history')loadHistory();
  if(name==='files')loadFiles('');
  if(name==='diag')loadDiag();
}

// ── API ──
async function api(path,opts){const r=await fetch(path,opts);if(!r.ok&&r.status!==202)throw new Error(await r.text());return r.json()}

// ── boot ──
async function boot(){
  try{
    const data=await api('/api/models');
    models=data.models;
    const planner=document.getElementById('planningModel');
    models.forEach(m=>{const option=document.createElement('option');option.value=m.label;option.textContent=m.label+(m.keyReady?'':'（凭据未就绪）');planner.appendChild(option)});
    document.getElementById('conn').classList.add('up');
    document.getElementById('connText').textContent='已连接 · 内核就绪';
    renderModels();loadHistory();loadConfig();loadKeys();
  }catch(e){document.getElementById('connText').textContent='连接失败'}
}

// ── models ──
function renderModels(){
  const el=document.getElementById('modelList');el.innerHTML='';
  models.forEach(m=>{
    const d=document.createElement('div');
    d.className='model-item'+(selModel===m.label?' sel':'');
    d.onclick=()=>{selModel=(selModel===m.label?null:m.label);renderModels()};
    d.innerHTML=`<div class="name">${m.label}<span class="tier ${m.tier}">${m.tier}</span></div>
      <div class="meta"><span class="key-status ${m.keyReady?'ok':'missing'}">${m.keyReady?'● key 就绪':'○ key 缺失'}</span> · ${m.note}</div>`;
    el.appendChild(d);
  });
}

// ── keys ──
async function loadKeys(){
  try{
    const d=await api('/api/diag');
    const el=document.getElementById('keyList');el.innerHTML='';
    for(const[k,v] of Object.entries(d.env_status||{})){
      const d2=document.createElement('div');d2.className='field';
      d2.innerHTML=`<label class="key-status ${v?'ok':'missing'}">${v?'●':'○'} ${k}</label>`;
      el.appendChild(d2);
    }
  }catch(e){document.getElementById('keyList').innerHTML='<div class="empty">加载失败</div>'}
}

// ── config snapshot ──
async function loadConfig(){
  try{
    const c=await api('/api/config');
    const el=document.getElementById('configView');
    let h='<div style="font-size:11px;color:var(--muted);margin-bottom:6px">base.json 当前配置快照</div>';
    const sections=[
      ['policy','权限','mode','sandbox','allow','ask','deny'],
      ['loop','循环','maxSteps','maxDepth','spawnBudget'],
      ['model','路由','primary','fallback','moa','strategy'],
      ['thinking','思考','mode'],
    ];
    sections.forEach(([id,label,...keys])=>{
      const cfg=c[id]||{};
      h+=`<div style="margin-bottom:6px;font-size:10px;color:var(--accent)">${label}</div>`;
      keys.forEach(k=>{
        const v=cfg[k];
        if(v!==undefined&&v!==null){
          const txt=Array.isArray(v)?JSON.stringify(v):typeof v==='object'?JSON.stringify(v):String(v);
          h+=`<div class="kv"><span class="k">${k}</span><span class="v">${esc(txt)}</span></div>`;
        }
      });
    });
    el.innerHTML=h;
  }catch(e){document.getElementById('configView').innerHTML='<div class="empty">加载失败</div>'}
}

// ── run ──
function collectParams(){
  const p={};
  p.model=selModel||null;
  const s=document.getElementById('strategy').value;p.strategy=s||null;
  const t=document.getElementById('thinking').value;p.thinking=t||null;
  const b=document.getElementById('bundle').value;p.bundle=b||null;
  const pm=document.getElementById('policyMode').value;p.policy_mode=pm||null;
  p.planning=document.getElementById('planning').value;
  p.planning_model=document.getElementById('planningModel').value;
  const ms=document.getElementById('maxSteps').value;if(ms)p.max_steps=parseInt(ms);
  const md=document.getElementById('maxDepth').value;if(md)p.max_depth=parseInt(md);
  const sb=document.getElementById('spawnBudget').value;if(sb)p.spawn_budget=parseInt(sb);
  return p;
}

async function startRun(){
  const task=document.getElementById('task').value.trim();
  if(!task)return alert('请输入任务');
  const body={task,...collectParams()};
  document.getElementById('runBtn').disabled=true;
  document.getElementById('cancelBtn').style.display='';
  document.getElementById('runState').textContent='提交中…';
  document.getElementById('stream').innerHTML='';
  document.getElementById('deliveries').innerHTML='';
  document.getElementById('usage').style.display='none';
  try{
    const{run_id}=await api('/api/run',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});
    curRunId=run_id;listenSSE(run_id);
  }catch(e){
    document.getElementById('runState').textContent='失败: '+e.message;
    document.getElementById('runBtn').disabled=false;
    document.getElementById('cancelBtn').style.display='none';
  }
}

function listenSSE(runId){
  if(evtSource)evtSource.close();
  evtSource=new EventSource('/api/run/'+runId+'/events');
  evtSource.onmessage=(e)=>{
    let d;try{d=JSON.parse(e.data)}catch{return}
    if(d.type==='step')appendStep(d);
    else if(d.type==='task_plan')renderPlan(d.plan);
    else if(d.type==='state'){
      document.getElementById('runState').textContent={running:'执行中…',done:'完成',error:'失败',cancelled:'已取消',cancelling:'取消中…'}[d.state]||d.state;
      if(d.state==='done'){renderFinal(d);renderDelivery(d.delivery||[]);renderUsage(d)}
      if(d.state==='error')renderError(d);
      if(d.state==='done'||d.state==='error'||d.state==='cancelled'){
        document.getElementById('runBtn').disabled=false;document.getElementById('cancelBtn').style.display='none';
        evtSource.close();evtSource=null;
      }
    }
  };
  evtSource.onerror=()=>{if(evtSource){evtSource.close();evtSource=null}pollRun(runId)};
}
function pollRun(runId){
  const iv=setInterval(async()=>{
    try{
      const r=await api('/api/run/'+runId);renderFromRec(r);
      if(r.state==='done'||r.state==='error'||r.state==='cancelled'){clearInterval(iv);document.getElementById('runBtn').disabled=false;document.getElementById('cancelBtn').style.display='none'}
    }catch(e){clearInterval(iv)}
  },1500);
}
function renderFromRec(rec){
  const s=document.getElementById('stream');s.innerHTML='';
  if(rec.planning)renderPlan(rec.planning);
  (rec.steps||[]).forEach(step=>appendStep(step));
  if(rec.state==='done')renderFinal({text:rec.text,usage:rec.usage,delivery:rec.delivery});
  if(rec.state==='error')renderError({error:rec.error,traceback:rec.traceback});
  if(rec.state==='cancelled'){const d=document.createElement('div');d.className='msg cancelled';d.innerHTML='<div class="head">已取消</div>';s.appendChild(d)}
  renderDelivery(rec.delivery||[]);renderUsage(rec);
  document.getElementById('runState').textContent={running:'执行中…',done:'完成',error:'失败',cancelled:'已取消'}[rec.state]||rec.state;
}
function appendStep(s){
  const d=document.createElement('div');d.className='msg tool';
  d.innerHTML=`<div class="head"><span class="idx">${s.index}</span>${esc(s.tool||'reason')} → ${esc(s.decision||'')}</div><div class="body">${esc(s.note||s.result||'')}</div>`;
  document.getElementById('stream').appendChild(d);d.scrollIntoView({behavior:'smooth',block:'end'});
}
function renderPlan(plan){const id='currentPlan';let box=document.getElementById(id);if(!box){box=document.createElement('div');box.id=id;box.className='msg';document.getElementById('stream').prepend(box)}box.innerHTML='<div class="head">执行规划</div><div class="body">'+esc((plan.tasks||[]).map((t,i)=>(i+1)+'. '+t).join('\n'))+'</div>'}
function renderFinal(d){if(d.planning)renderPlan(d.planning);if(!d.text)return;const b=document.createElement('div');b.className='final-box';b.innerHTML=`<h4>最终回复</h4><div class="body">${esc(d.text)}</div>`;document.getElementById('stream').appendChild(b);b.scrollIntoView({behavior:'smooth',block:'end'})}
function renderError(d){const b=document.createElement('div');b.className='msg error';b.innerHTML=`<div class="head">错误</div><div class="body">${esc(d.error||'')}${d.traceback?'\n\n'+esc(d.traceback.slice(0,800)):''}</div>`;document.getElementById('stream').appendChild(b)}
function renderUsage(d){if(!d.usage)return;const bar=document.getElementById('usage');bar.style.display='flex';document.getElementById('uTok').textContent=d.usage&&d.usage.total_tokens!==undefined?d.usage.total_tokens:'—';document.getElementById('uStep').textContent=d.steps?d.steps.length:'—';const s=d.finished&&d.created?Math.max(0,Math.round(d.finished-d.created)):0;document.getElementById('uTime').textContent=s+'s';document.getElementById('uModel').textContent=d.model||selModel||'—'}
function renderDelivery(items){const dl=document.getElementById('deliveries');dl.innerHTML='';if(!items||!items.length){dl.innerHTML='<div class="empty">本次无独立交付文件</div>';return}items.forEach(item=>{const d=document.createElement('div');d.className='deliver-item';if(item.kind==='file'){d.innerHTML=`<div class="dtype">${esc(item.ext||'file')}</div><div class="dname">${esc(item.path)}</div><div class="dmeta">${fmtSize(item.size)} · ${fmtTime(item.mtime)}</div><div class="btn-row"><button class="sm" data-vpath="${escAttr(item.path)}">查看</button></div>`}else{d.innerHTML=`<div class="dtype">代码 · ${esc(item.lang)}</div><pre>${esc(item.preview)}</pre>`}dl.appendChild(d)});dl.addEventListener('click',e=>{const btn=e.target.closest('[data-vpath]');if(btn)viewFile(btn.dataset.vpath)})}
async function cancelRun(){if(!curRunId)return;try{await api('/api/run/'+curRunId+'/cancel',{method:'POST'})}catch(e){}}

// ── history ──
async function loadHistory(){try{const{runs}=await api('/api/run');const el=document.getElementById('historyList');el.innerHTML='';if(!runs.length){el.innerHTML='<div class="empty">暂无历史</div>';return}runs.forEach(r=>{const d=document.createElement('div');d.className='hist-item';d.onclick=()=>{curRunId=r.id;switchTab('run',document.querySelector('[data-tab=run]'));renderFromRec(r)};d.innerHTML=`<div class="hhead"><span class="hstate ${r.state}">${r.state}</span><span style="font-size:10px;color:var(--muted)">${fmtTime(r.created)}</span></div><div class="htask">${esc(r.task)}</div><div class="hmeta">${r.model||'auto'} · ${r.bundle||'base'} · ${(r.steps||[]).length} steps</div>`;el.appendChild(d)})}catch(e){}}

// ── files ──
async function loadFiles(sub){try{const{files}=await api('/api/files'+(sub?'?path='+encodeURIComponent(sub):''));const el=document.getElementById('fileList');el.innerHTML='';if(!files.length){el.innerHTML='<div class="empty">空目录</div>';return}files.forEach(f=>{const d=document.createElement('div');d.className='file-item';if(f.type==='dir'){d.innerHTML=`<span class="icon">📁</span><span class="fname">${esc(f.name)}</span>`;d.onclick=()=>loadFiles(f.path)}else{d.innerHTML=`<span class="icon">${esc({'.py':'🐍','.js':'📜','.html':'📄','.md':'📝','.json':'📦'}[f.ext]||'📎')}</span><span class="fname">${esc(f.name)}</span><span class="fsize">${fmtSize(f.size)}</span>`;d.onclick=()=>viewFile(f.path)}el.appendChild(d)})}catch(e){document.getElementById('fileList').innerHTML='<div class="empty">失败</div>'}}
async function viewFile(path){const el=document.getElementById('fileContent');try{const r=await api('/api/file?path='+encodeURIComponent(path));if(r.error){el.innerHTML=`<div class="empty">${esc(r.error)}</div>`;return}if(r.binary){el.innerHTML=`<div class="empty">二进制 ${fmtSize(r.size)}</div>`;return}el.innerHTML=`<div class="meta">${esc(r.path)} · ${fmtSize(r.size)}</div><pre>${esc(r.text)}</pre>`}catch(e){el.innerHTML='<div class="empty">失败</div>'}}

// ── diag ──
async function loadDiag(){try{const d=await api('/api/diag');const el=document.getElementById('diagPanel');let h='';h+=kv('forge home',d.forge_home);h+=kv('secrets.json',d.keys_exists?'✅ 存在':'❌ 缺失');h+=kv('workspace',d.workspace);h+=kv('version',d.forge_version);h+='<div style="margin:12px 0 6px;font-size:11px;color:var(--muted)">KEY 注入</div>';for(const[k,v]of Object.entries(d.env_status||{})){h+=kv(k,v?'✅':'❌')}el.innerHTML=h}catch(e){document.getElementById('diagPanel').innerHTML='失败'}}

function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function escAttr(s){return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#39;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function fmtSize(n){return n>1048576?(n/1048576).toFixed(1)+' MB':n>1024?(n/1024).toFixed(1)+' KB':n+' B'}
function fmtTime(t){return t?new Date(t*1000).toLocaleString():''}
function kv(k,v){return `<div class="kv"><span class="k">${esc(k)}</span><span class="v">${esc(String(v))}</span></div>`}

boot();
// 点击折叠面板任意标题栏展开全部
document.addEventListener('keydown',e=>{if(e.key==='`')toggleAll(document.querySelector('.section-head').classList.contains('open')?false:true)});
