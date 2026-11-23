let me=null,csrf='',editingServerId=null,selectedServerRoom=null;const $=s=>document.querySelector(s),esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function showAuth(setup){$('#app').classList.add('hidden');$('#login').classList.remove('hidden');$('#setupHint').classList.toggle('hidden',!setup);$('#authTitle').textContent=setup?'首次使用：创建管理员':'登录 IDC资产管理';$('#authSubmit').textContent=setup?'创建管理员':'登录';$('#authForm').dataset.setup=setup?'1':'';$('#authForm').elements.password.minLength=setup?12:1;$('#authForm').elements.password.autocomplete=setup?'new-password':'current-password'}
function todayStr(){const d=new Date(),p=n=>String(n).padStart(2,'0');return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())}
function initMoveDate(){const el=$('#moveForm')&&$('#moveForm').elements.moved_at;if(el&&!el.value)el.value=todayStr()}

async function start(){
  try{
    let status=await api('/api/status');
    if(status.setup_required)return showAuth(true);
    let r=await api('/api/me');setUser(r);await refresh();
    const box=$('#bootError');if(box)box.classList.add('hidden');
  }catch(e){
    if(e&&e.status>=401&&e.status<500)return showAuth(false); // 明确未登录
    const box=$('#bootError');
    if(box){
      $('#app').classList.add('hidden');$('#login').classList.add('hidden');
      $('#bootErrorMsg').textContent=(e&&e.message)||'未知错误';
      box.classList.remove('hidden');
    }
  }
}
function setUser(u){me=u;csrf=u.csrf;$('#login').classList.add('hidden');$('#app').classList.remove('hidden');$('#who').innerHTML=`${esc(u.username)} <span class="badge">${u.is_admin?'管理员':(u.can_edit?'协管员 · 编辑':'协管员 · 查看')}</span>`;$('#usersTab').classList.toggle('hidden',!u.is_admin);$('#recycleTab').classList.toggle('hidden',!u.is_admin);$('#itemFormCard').classList.toggle('hidden',!u.can_edit&&!u.is_admin);$('#importCard').classList.toggle('hidden',!u.can_edit&&!u.is_admin);$('#moveFormCard').classList.toggle('hidden',!u.can_edit&&!u.is_admin);$('#movementImportCard').classList.toggle('hidden',!u.can_edit&&!u.is_admin);$('#serverImportCard').classList.toggle('hidden',!u.can_edit&&!u.is_admin);$('#serverComponentImportCard').classList.toggle('hidden',!u.can_edit&&!u.is_admin);document.querySelectorAll('.serverActionHeader').forEach(el=>el.classList.toggle('hidden',!u.can_edit&&!u.is_admin));$('#itemActionHeader').classList.toggle('hidden',!u.is_admin&&!u.can_edit);$('#movementActionHeader').classList.toggle('hidden',!u.is_admin);initMoveDate()}
$('#authForm').onsubmit=async e=>{e.preventDefault();$('#authMsg').textContent='';let f=new FormData(e.target),payload=Object.fromEntries(f);try{let u=await api(e.target.dataset.setup?'/api/setup':'/api/login',{method:'POST',body:JSON.stringify(payload)});setUser(u);await refresh()}catch(err){$('#authMsg').textContent=err.message}};
async function refresh(){await loadItems();await loadMoves();await loadServers();await loadSpares();if(me.is_admin)await loadUsers()}
let editingMovementId=null;
function movementRow(x,editing=false){
 if(!editing)return `<tr><td>${esc(x.moved_at)}</td><td>${esc(x.kind)}</td><td>${esc(x.category)}</td><td>${esc(x.model)}</td><td>${x.qty}</td><td>${esc(x.location)}</td><td>${esc(x.owner)}</td><td>${esc(x.notes)}</td>${me.can_edit||me.is_admin?`<td><button class="secondary" onclick="editMovement(${x.id})">编辑</button> ${me.is_admin?`<button class="danger" onclick="deleteMovement(${x.id})">删除</button>`:''}</td>`:'<td></td>'}</tr>`;
 let kindOpts=['入库','领用/安装','报废'].map(v=>`<option ${v===x.kind?'selected':''}>${v}</option>`).join('');
 let modelOpts=(window.allItems||[]).map(i=>`<option value="${esc(i.model)}" ${i.model===x.model?'selected':''}>${esc(i.model)} · ${esc(i.category)}</option>`).join('');
 return `<tr id="movement-${x.id}"><td><input name="moved_at" type="date" value="${esc(x.moved_at)}"></td><td><select name="kind">${kindOpts}</select></td><td>${esc(x.category)}</td><td><select name="model">${modelOpts}</select></td><td><input name="qty" type="number" min="1" step="1" value="${x.qty}"></td><td><input name="location" value="${esc(x.location)}"></td><td><input name="owner" value="${esc(x.owner)}"></td><td><input name="notes" value="${esc(x.notes)}"></td><td><button onclick="saveMovement(${x.id})">保存</button> <button class="secondary" onclick="cancelMovementEdit()">取消</button><div class="status edit-error"></div></td></tr>`;
}
async function loadMoves(){let {movements}=await api('/api/movements');window.allMovements=movements;$('#movesBody').innerHTML=movements.map(x=>movementRow(x,x.id===editingMovementId)).join('')}
function editMovement(id){editingMovementId=id;loadMoves()}
function cancelMovementEdit(){editingMovementId=null;loadMoves()}
async function saveMovement(id){let tr=$('#movement-'+id);let payload={};for(let k of ['moved_at','kind','model','qty','location','owner','notes'])payload[k]=tr.querySelector(`[name="${k}"]`).value;payload.qty=Number(payload.qty);try{await api('/api/movements/'+id,{method:'PATCH',body:JSON.stringify(payload)});editingMovementId=null;await loadItems();await loadMoves()}catch(e){tr.querySelector('.edit-error').textContent=e.message}}

const importFieldNames={category:'类别',model:'型号',description:'名称/规格',planned:'计划数量',supplier:'供应商/品牌',location:'存放位置',owner:'负责人',notes:'备注'};
$('#importForm').onsubmit=async e=>{e.preventDefault();let file=e.target.elements.file.files[0];if(!file)return;$('#importMsg').textContent='正在读取并检查表格…';$('#importPreview').innerHTML='';try{let r=await fetch('/api/import-preview',{method:'POST',headers:{'Content-Type':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','X-CSRF-Token':csrf,'X-Category-Default':e.target.elements.default_category.value},body:file});let d=await r.json();if(!r.ok)throw Error(d.error||'无法读取表格');renderImportPreview(d)}catch(err){$('#importMsg').textContent=err.message}};

$('#itemForm').onsubmit=async e=>{e.preventDefault();let p=Object.fromEntries(new FormData(e.target));try{await api('/api/items',{method:'POST',body:JSON.stringify(p)});e.target.reset();e.target.elements.planned.value=0;$('#itemMsg').textContent='物料已保存';await loadItems();filterItems()}catch(err){$('#itemMsg').textContent=err.message}};

const movementFieldNames={model:'型号',category:'类别',qty:'入库数量',date:'日期',location:'位置/去向',owner:'负责人',notes:'备注',kind:'业务类型'};
$('#movementImportForm').onsubmit=async e=>{e.preventDefault();let file=e.target.elements.file.files[0];if(!file)return;$('#movementImportMsg').textContent='正在读取并校验入库明细…';$('#movementImportPreview').innerHTML='';try{let r=await fetch('/api/import-movements-preview',{method:'POST',headers:{'Content-Type':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','X-CSRF-Token':csrf,'X-Category-Default':e.target.elements.default_category.value},body:file});let d=await r.json();if(!r.ok)throw Error(d.error||'无法读取表格');renderMovementImportPreview(d)}catch(err){$('#movementImportMsg').textContent=err.message}};

$('#moveForm').elements.kind.onchange=e=>{$('#locationLabel').textContent=e.target.value==='入库'?'入库位置（选填）':e.target.value==='报废'?'处理去向（选填）':'使用位置/目的地（选填）'};
function fillMovementModels(items=window.allItems||[]){let cat=$('#moveCategory')?.value;let matches=items.filter(x=>x.category===cat);$('#moveModel').innerHTML=matches.map(x=>`<option value="${esc(x.model)}">${esc(x.model)}</option>`).join('')}$('#moveCategory').onchange=()=>fillMovementModels();
$('#moveForm').onsubmit=async e=>{
  e.preventDefault();
  const f=e.target,p=Object.fromEntries(new FormData(f));
  const miss=[];
  if(!p.moved_at)miss.push('日期');
  if(!p.qty||!Number.isInteger(Number(p.qty))||!(Number(p.qty)>0))miss.push('数量（需大于 0 的整数）');
  if(!p.model)miss.push('型号');
  if(miss.length){$('#moveMsg').textContent='请填写：'+miss.join('、');return}
  $('#moveMsg').textContent='';
  try{await api('/api/movements',{method:'POST',body:JSON.stringify(p)});$('#moveMsg').textContent='库存明细已登记';f.reset();f.elements.moved_at.value=todayStr();await loadItems();fillMovementModels();await loadMoves()}
  catch(err){$('#moveMsg').textContent=err.message}
};
function filterItems(){
  const q=$('#itemSearch').value.trim().toLowerCase(),cat=$('#categoryFilter').value;
  const body=$('#itemsBody');if(!body)return;
  let shown=0,total=0;
  for(const tr of Array.from(body.rows)){
    if(tr.dataset.filterHint)continue;
    total++;
    const hit=tr.innerText.toLowerCase().includes(q)&&(!cat||tr.dataset.category===cat);
    tr.hidden=!hit;if(hit)shown++;
  }
  let hint=body.querySelector('[data-filter-hint]');
  if(shown===0&&total>0){
    if(!hint){hint=document.createElement('tr');hint.dataset.filterHint='1';hint.innerHTML='<td colspan="'+ITEM_COL_COUNT+'" class="filter-empty">没有匹配的物料 · 共 '+total+' 条，匹配 0 条。 <button type="button" class="linklike" onclick="clearItemFilter()">清除筛选</button></td>';body.appendChild(hint)}
    else hint.innerHTML='<td colspan="'+ITEM_COL_COUNT+'" class="filter-empty">没有匹配的物料 · 共 '+total+' 条，匹配 0 条。 <button type="button" class="linklike" onclick="clearItemFilter()">清除筛选</button></td>';
    hint.hidden=false;
  }else if(hint)hint.hidden=true;
}
function clearItemFilter(){const s=$('#itemSearch'),c=$('#categoryFilter');if(s)s.value='';if(c)c.value='';filterItems()}
$('#itemSearch').oninput=filterItems;$('#categoryFilter').onchange=filterItems;
async function deleteMovement(id){if(!await uiConfirm('确定删除这条库存变动明细吗？删除后台账汇总库存会随之变化。'))return;try{await api('/api/movements/'+id,{method:'DELETE'});await loadItems();await loadMoves()}catch(e){uiToast(e.message)}}
async function activateTab(tab){document.querySelectorAll('.tabs button').forEach(x=>x.classList.toggle('active',x===tab));
try{if(tab&&tab.scrollIntoView)tab.scrollIntoView({behavior:'smooth',block:'nearest',inline:'center'})}catch(e){}document.querySelectorAll('.pane').forEach(x=>x.classList.toggle('hidden',x.id!==tab.dataset.tab));try{if(tab.dataset.tab==='items'){await loadItems();filterItems()}else if(tab.dataset.tab==='movements'){await Promise.all([loadItems(),loadMoves()]);filterItems();fillMovementModels()}else if(tab.dataset.tab==='servers')await loadServers();else if(tab.dataset.tab==='spares')await loadSpares();else if(tab.dataset.tab==='recycle'&&me?.is_admin)await loadRecycle();else if(tab.dataset.tab==='users'&&me?.is_admin)await loadUsers()}catch(e){uiToast('刷新页面数据失败：'+e.message)}}
document.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>activateTab(b));
$('#userForm').onsubmit=async e=>{e.preventDefault();let f=new FormData(e.target);let p={username:f.get('username'),password:f.get('password'),can_view:f.has('can_view'),can_edit:f.has('can_edit')};try{await api('/api/users',{method:'POST',body:JSON.stringify(p)});e.target.reset();$('#userMsg').textContent='协管员账号已创建';await loadUsers()}catch(err){$('#userMsg').textContent=err.message}};
async function loadUsers(){let {users}=await api('/api/users');$('#usersBody').innerHTML=users.filter(x=>!x.is_admin).map(x=>`<tr><td>${esc(x.username)}</td><td><input type="checkbox" ${x.can_view?'checked':''} data-id="${x.id}" data-key="can_view"></td><td><input type="checkbox" ${x.can_edit?'checked':''} data-id="${x.id}" data-key="can_edit"></td><td>${x.active?'启用':'停用'}</td><td><button class="secondary" data-toggle="${x.id}" data-active="${x.active?0:1}">${x.active?'停用账号':'启用账号'}</button></td></tr>`).join('');$('#usersBody').querySelectorAll('input').forEach(el=>el.onchange=async()=>{let id=el.dataset.id,other=$(`#usersBody input[data-id="${id}"][data-key="${el.dataset.key==='can_view'?'can_edit':'can_view'}"]`);try{await api('/api/users/'+id,{method:'PATCH',body:JSON.stringify({can_view:el.dataset.key==='can_view'?el.checked:other.checked,can_edit:el.dataset.key==='can_edit'?el.checked:other.checked,active:true})})}catch(e){uiToast(e.message);await loadUsers()}});$('#usersBody').querySelectorAll('button[data-toggle]').forEach(b=>b.onclick=async()=>{let id=b.dataset.toggle,view=$(`#usersBody input[data-id="${id}"][data-key="can_view"]`).checked,edit=$(`#usersBody input[data-id="${id}"][data-key="can_edit"]`).checked;try{await api('/api/users/'+id,{method:'PATCH',body:JSON.stringify({can_view:view,can_edit:edit,active:b.dataset.active==='1'})});await loadUsers()}catch(e){uiToast(e.message)}})}
async function logout(){
  try{await api('/api/logout',{method:'POST',body:'{}'})}
  catch(e){
    if(e&&e.status===401){/* 会话已失效，视为已退出 */}
    else{uiToast('退出失败，请检查网络后重试');return}
  }
  csrf='';me=null;showAuth(false)
}

async function loadSpares(){window.spareData=await api('/api/spares');renderSpareMaterials();renderMaterialSpares();renderServerSpares()}
function renderSpareMaterials(){let d=window.spareData||{materials:[]},q=($('#spareMaterialSearch').value||'').trim().toLowerCase(),items=d.materials.filter(x=>`${x.category} ${x.model} ${x.description}`.toLowerCase().includes(q)),available=items.reduce((a,x)=>a+Math.max(0,x.available),0),damaged=items.reduce((a,x)=>a+x.damaged,0),pending=(d.material_spares||[]).filter(x=>x.status==='待处理').length;$('#spareMaterialStats').innerHTML=`<div class="server-stat"><b>${items.length}</b><span>种型号</span></div><div class="server-stat"><b>${available}</b><span>可用库存</span></div><div class="server-stat"><b>${damaged}</b><span>累计报废</span></div><div class="server-stat"><b>${pending}</b><span>待补SN坏件</span></div>`;$('#spareMaterialsBody').innerHTML=items.map(x=>`<tr><td>${esc(x.category)}</td><td>${esc(x.model)}</td><td>${esc(x.description)}</td><td><b>${x.available}</b></td><td>${x.damaged}</td></tr>`).join('')||'<tr><td colspan="5" class="hint">暂无库存或报废物料</td></tr>'}
function renderMaterialSpares(){let d=window.spareData||{material_spares:[],material_statuses:[]},filter=$('#materialSpareFilter').value,search=($('#spareMaterialSearch').value||'').trim().toLowerCase(),rows=(d.material_spares||[]).filter(x=>(!filter||x.status===filter)&&(`${x.category} ${x.model} ${x.description}`.toLowerCase().includes(search))),canEdit=me&&(me.can_edit||me.is_admin);if($('#materialSpareFilter').options.length===1)for(let state of d.material_statuses)$('#materialSpareFilter').add(new Option(state,state));$('#materialSparesBody').innerHTML=rows.map(x=>{let options=d.material_statuses.filter(state=>state!=='待处理').map(state=>`<option ${state===x.status?'selected':''}>${esc(state)}</option>`).join(''),history=x.history.map(h=>`<li><span>${esc(h.old_serial_number||'（未录SN）')} → ${esc(h.serial_number)} · ${esc(h.from_status)} → ${esc(h.to_status)}</span><small>操作账号：${esc(h.operator_username||'未知')} · 实际操作人：${esc(h.responsible||'')} · ${esc(h.operated_at)}${h.notes?' · '+esc(h.notes):''}</small></li>`).join(''),source=x.source_location||'—';return `<tr data-material-spare="${x.id}"><td>${esc(x.moved_at||'')}</td><td>${esc(x.category)} / <code>${esc(x.model)}</code></td><td>${x.unit_index} / 来源单 #${x.movement_id||'—'}</td><td>${canEdit?`<input name="serial_number" value="${esc(x.serial_number)}" placeholder="待处理时必填SN">`:esc(x.serial_number||'待补录')}</td><td>${canEdit?`<select name="status"><option value="待处理" ${x.status==='待处理'?'selected':''}>待处理</option>${options}</select>`:`<span class="badge">${esc(x.status)}</span>`}</td><td>${esc(source)} / ${esc(x.source_owner||'')}</td><td>${canEdit?`<input name="responsible" value="${esc(x.responsible)}" placeholder="实际操作人">`:esc(x.responsible||'')}</td><td>${canEdit?`<input name="notes" value="${esc(x.notes)}" placeholder="备注">`:esc(x.notes)}</td><td>${canEdit?`<button class="secondary" onclick="saveMaterialSpare(${x.id})">保存</button> `:''}<details><summary>历史（${x.history.length}）</summary><ol>${history||'<li>暂无处理历史</li>'}</ol></details></td></tr>`}).join('')||'<tr><td colspan="9" class="hint">暂无报废坏件；从库存变动明细登记报废后会自动出现在这里。</td></tr>'}
async function saveMaterialSpare(id){let tr=`#materialSparesBody tr[data-material-spare="${id}"]`;let row=$(tr);try{await api('/api/material-spares/'+id,{method:'PATCH',body:JSON.stringify({serial_number:row.querySelector('[name="serial_number"]').value.trim(),status:row.querySelector('[name="status"]').value,responsible:row.querySelector('[name="responsible"]').value.trim(),notes:row.querySelector('[name="notes"]').value.trim()})});await loadSpares()}catch(e){uiToast(e.message)}}
function renderServerSpares(){let d=window.spareData||{server_spares:[],statuses:[]},filter=$('#serverSpareFilter').value,rows=d.server_spares.filter(x=>!filter||x.status===filter),canEdit=me&&(me.can_edit||me.is_admin),isAdmin=me&&me.is_admin;if($('#serverSpareFilter').options.length===1)for(let state of d.statuses)$('#serverSpareFilter').add(new Option(state,state));let counts=Object.fromEntries(d.statuses.map(state=>[state,d.server_spares.filter(x=>x.status===state).length])),orphanCount=d.server_spares.filter(x=>x.orphan).length;$('#serverSpareStats').innerHTML=`<div class="server-stat"><b>${d.server_spares.length}</b><span>更换部件</span></div><div class="server-stat"><b>${counts['待检测']||0}</b><span>待检测</span></div><div class="server-stat"><b>${counts['待RMA']||0}</b><span>待RMA</span></div><div class="server-stat"><b>${counts['报废']||0}</b><span>报废</span></div><div class="server-stat"><b>${orphanCount}</b><span>已归档（原服务器已删除）</span></div>`;$('#serverSparesBody').innerHTML=rows.map(x=>{let opts=d.statuses.map(state=>`<option ${state===x.status?'selected':''}>${esc(state)}</option>`).join(''),history=(x.history||[]).map(h=>`<li><span>${esc(h.from_status)} → ${esc(h.to_status)}</span><small>操作账号：${esc(h.operator_username||'未知')} · 实际操作人：${esc(h.responsible)} · ${esc(h.operated_at)}${h.notes?' · '+esc(h.notes):''}</small></li>`).join(''),snCell=`<code>${esc(x.current_sn||x.server_sn)}</code>${x.orphan?' <span class="badge">原服务器已删除</span>':''}`,delBtn=(isAdmin&&x.orphan)?`<button class="secondary danger" onclick="deleteServerSpare(${x.id})">彻底删除</button> `:'';return `<tr data-spare-id="${x.id}"><td>${esc(x.room)} / ${esc(x.rack)}</td><td>${snCell}</td><td>U${x.node_u}</td><td>${esc(x.kp_name)}</td><td><code>${esc(x.node_sn)}</code></td><td>${canEdit?`<select name="status">${opts}</select>`:`<span class="badge">${esc(x.status)}</span>`}</td><td>${canEdit?`<input name="responsible" value="${esc(x.responsible)}" placeholder="实际操作人">`:esc(x.responsible)}</td><td>${canEdit?`<input name="notes" value="${esc(x.notes)}" placeholder="备注（最多 500 字）">`:esc(x.notes)}</td><td>${canEdit?`<button class="secondary" onclick="saveServerSpare(${x.id})">保存</button> `:''}${delBtn}<details><summary>历史（${(x.history||[]).length}）</summary><ol>${history||'<li>暂无状态历史</li>'}</ol></details></td></tr>`}).join('')||'<tr><td colspan="9" class="hint">暂时没有服务器更换部件；更换任意有 SN 的 U 位部件后，旧 SN 会自动登记。</td></tr>'}
async function saveServerSpare(id){let tr=`#serverSparesBody tr[data-spare-id="${id}"]`,row=$(tr);try{await api('/api/server-spares/'+id,{method:'PATCH',body:JSON.stringify({status:row.querySelector('[name="status"]').value,responsible:row.querySelector('[name="responsible"]').value.trim(),notes:row.querySelector('[name="notes"]').value.trim()})});await loadSpares()}catch(e){uiToast(e.message)}}
$('#spareMaterialSearch').oninput=()=>{renderSpareMaterials();renderMaterialSpares()};$('#materialSpareFilter').onchange=renderMaterialSpares;$('#serverSpareFilter').onchange=renderServerSpares;
async function loadServers(){let {servers}=await api('/api/servers');window.allServers=servers;renderServerMap()}
function renderServerMap(){let all=window.allServers||[],q=($('#serverSearch').value||'').trim().toLowerCase(),filtered=all.filter(x=>`${x.room} ${x.rack} ${x.sn}`.toLowerCase().includes(q)),rooms=[...new Set(all.map(x=>x.room))].sort(),totalRacks=new Set(all.map(x=>x.room+'\u0000'+x.rack)).size;$('#serverStats').innerHTML=`<div class=\"server-stat\"><b>${all.length}</b><span>台 GB300</span></div><div class=\"server-stat\"><b>${totalRacks}</b><span>个机柜位置</span></div><div class=\"server-stat\"><b>${rooms.length}</b><span>个机房</span></div><div class=\"server-stat muted-stat\"><b>${filtered.length}</b><span>当前匹配服务器</span></div>`;$('#serverOverview').classList.toggle('hidden',!!selectedServerRoom);$('#serverInterior').classList.toggle('hidden',!selectedServerRoom);$('#serverAssetsCard').classList.toggle('hidden',!selectedServerRoom);if(!selectedServerRoom){let visibleRooms=rooms.filter(room=>filtered.some(x=>x.room===room));$('#serverOverview').innerHTML=visibleRooms.map(room=>{let roomServers=all.filter(x=>x.room===room),matches=filtered.filter(x=>x.room===room),racks=new Set(roomServers.map(x=>x.rack)),markers=Array.from({length:Math.min(racks.size,24)},()=>'<i></i>').join('');return `<button class=\"room-card\" data-room=\"${esc(room)}\"><span class=\"room-schematic\"><span class=\"room-schematic-label\">${esc(room)} · 机房</span><span class=\"room-schematic-racks\">${markers}</span><span class=\"room-schematic-door\">入口</span></span><span class=\"room-card-info\"><strong>${esc(room)} 机房</strong><span>${roomServers.length} 台服务器 · ${racks.size} 个机柜位置${q?` · 匹配 ${matches.length} 台`:''}</span></span></button>`}).join('')||'<p class=\"hint\">没有符合条件的机房或服务器</p>';document.querySelectorAll('.room-card').forEach(card=>card.onclick=()=>{selectedServerRoom=card.dataset.room;renderServerMap()});return}let roomServers=all.filter(x=>x.room===selectedServerRoom),roomFiltered=filtered.filter(x=>x.room===selectedServerRoom),keys=new Set(roomFiltered.map(x=>x.room+'\u0000'+x.rack)),racks=new Map();for(let x of roomServers){let key=x.room+'\u0000'+x.rack;if(!racks.has(key))racks.set(key,[]);racks.get(key).push(x)}let roomRackCount=new Set(roomServers.map(x=>x.rack)).size,maxPosition=Math.max(12,...roomServers.map(x=>Number((x.rack.match(/\d+$/)||[])[0]||0)));$('#serverRoomTitle').textContent=`${selectedServerRoom} 机房布局`;$('#serverAssetsTitle').textContent=`${selectedServerRoom} 服务器清单`;$('#serverRoomSummary').textContent=`${roomServers.length} 台服务器 · ${roomRackCount} 个机柜位置`+(q?` · 当前匹配 ${roomFiltered.length} 台`:'');let roomRacks=[...racks.entries()].filter(([k])=>keys.has(k));let letters=[...new Set(roomRacks.map(([k])=>k.split('\u0000')[1].match(/^[A-Za-z]+/)?.[0]||'?'))].sort();let rows=letters.map((letter,ri)=>{let tiles=Array.from({length:maxPosition-2},(_,i)=>{let rack=letter+String(i+3).padStart(2,'0'),key=selectedServerRoom+'\u0000'+rack,items=racks.get(key);return keys.has(key)?`<button class=\"rack-tile occupied\" data-room=\"${esc(selectedServerRoom)}\" data-rack=\"${esc(rack)}\"><span class=\"rack-name\">${esc(rack)}</span><span class=\"rack-unit\">${items.length} 台</span><span class=\"rack-server\">GB300</span></button>`:`<div class=\"rack-tile rack-empty\"><span>${esc(rack)}</span></div>`}).join('');return `<div class=\"rack-row ${ri===2?'row-gap':''}\"><div class=\"rack-label\">${esc(letter)} 排</div><div class=\"rack-cells\">${tiles}</div></div>`}).join('');$('#serverRooms').innerHTML=rows||'<p class=\"hint\">当前机房没有匹配的服务器位置</p>';document.querySelectorAll('.rack-tile.occupied').forEach(btn=>btn.onclick=()=>openRack(btn.dataset.room,btn.dataset.rack));renderServerAssets(roomFiltered)}
function renderServerAssets(rows){let canEdit=me&&(me.can_edit||me.is_admin);$('#serverAssetsBody').innerHTML=rows.map(x=>{let edit=editingServerId===x.id;if(edit)return `<tr id="server-row-${x.id}"><td><input data-field="room" maxlength="100" value="${esc(x.room)}"></td><td><input data-field="rack" maxlength="100" value="${esc(x.rack)}"></td><td><input data-field="sn" maxlength="100" value="${esc(x.sn)}"></td><td class="serverActionHeader"><div class="server-actions"><button onclick="saveServer(${x.id})">保存</button><button class="secondary" onclick="cancelServerEdit()">取消</button></div><div class="status server-edit-error"></div></td></tr>`;return `<tr><td>${esc(x.room)}</td><td>${esc(x.rack)}</td><td><code>${esc(x.sn)}</code></td><td class="serverActionHeader">${canEdit?`<div class="server-actions"><button class="secondary" onclick="editServer(${x.id})">编辑</button>${me.is_admin?`<button class="danger" onclick="deleteServer(${x.id})">删除</button>`:''}</div>`:''}</td></tr>`}).join('')||'<tr><td colspan="4" class="hint">该机房暂无 GB300 服务器记录</td></tr>';document.querySelectorAll('.serverActionHeader').forEach(el=>el.classList.toggle('hidden',!canEdit))}
function editServer(id){editingServerId=id;renderServerMap()}
function cancelServerEdit(){editingServerId=null;renderServerMap()}
async function saveServer(id){let tr=$('#server-row-'+id),payload={};for(let field of ['room','rack','sn'])payload[field]=tr.querySelector(`[data-field="${field}"]`).value;try{await api('/api/servers/'+id,{method:'PATCH',body:JSON.stringify(payload)});editingServerId=null;await loadServers()}catch(e){tr.querySelector('.server-edit-error').textContent=e.message}}
async function deleteServer(id){let row=(window.allServers||[]).find(x=>x.id===id);if(!row)return;if(!await uiConfirm(`确认删除服务器 ${row.sn}（${row.room} / ${row.rack}）？`))return;const attempt=confirmSn=>api('/api/servers/'+id,{method:'DELETE',body:JSON.stringify(confirmSn?{confirm_sn:confirmSn}:{})});try{await attempt('');await loadServers();uiToast('服务器已删除；更换历史与备件已归档','ok')}catch(e){const d=e.data||{};if(!d.requires_confirm){uiToast(e.message);return}const c=d.counts||{};const msg=`该服务器有 ${c.components||0} 条 46U 部件、${c.operations||0} 条更换/修正历史、${c.spares||0} 条备件及 ${c.history||0} 条状态历史。\n\n删除会移除部件清单，历史将归档为「原服务器已删除」（不会错挂到复用的 SN）。\n\n如确需删除，请输入完整 SN：${d.sn}`;const typed=await uiPrompt(msg,d.sn);if(typed===null)return;try{await attempt(typed.trim());await loadServers();uiToast('服务器已删除；更换历史与备件已归档','ok')}catch(e2){uiToast(e2.message)}}}
const serverFieldLabels={room:'机房/房间',rack:'机柜位置',sn:'SN'};
$('#serverImportForm').onsubmit=async e=>{e.preventDefault();let file=e.target.elements.file.files[0];if(!file)return;$('#serverImportMsg').textContent='正在识别并校验服务器表…';$('#serverImportPreview').innerHTML='';try{let r=await fetch('/api/import-servers-preview',{method:'POST',headers:{'Content-Type':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','X-CSRF-Token':csrf},body:file});let d=await r.json();if(!r.ok)throw Error(d.error||'无法读取表格');renderServerImportPreview(d)}catch(err){$('#serverImportMsg').textContent=err.message}};
function renderServerImportPreview(d){renderGbPreview('servers',d)}
async function confirmServerImport(token){return confirmGbImport('servers',token)}
const serverComponentFieldLabels={server_sn:'服务器 SN',node_sn:'部件 NODE_SN',node:'NODE / U 位',kp_name:'KP_NAME',bmc_mac:'BMC_MAC'};
$('#serverComponentImportForm').onsubmit=async e=>{e.preventDefault();let file=e.target.elements.file.files[0];if(!file)return;$('#serverComponentImportMsg').textContent='正在识别并校验 46U 部件清单…';$('#serverComponentImportPreview').innerHTML='';try{let r=await fetch('/api/import-server-components-preview',{method:'POST',headers:{'Content-Type':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','X-CSRF-Token':csrf},body:file});let d=await r.json();if(!r.ok)throw Error(d.error||'无法读取部件表');renderServerComponentImportPreview(d)}catch(err){$('#serverComponentImportMsg').textContent=err.message}};
function renderServerComponentImportPreview(d){renderGbPreview('components',d)}
async function confirmServerComponentImport(token){return confirmGbImport('components',token)}
function renderServerComponentStack(server,components,operations=[]){
 let byU=new Map(components.map(x=>[Number(x.node_u),x])),opsByU=new Map();
 for(let op of operations){let u=Number(op.node_u);if(!opsByU.has(u))opsByU.set(u,[]);opsByU.get(u).push(op)}
 let canEdit=me&&(me.can_edit||me.is_admin);
 let slots=Array.from({length:46},(_,i)=>46-i).map(u=>{
  let x=byU.get(u),ops=opsByU.get(u)||[];
  let history=ops.map(op=>{
   let oldSn='',newSn='';
   try{oldSn=JSON.parse(op.old_data||'{}').node_sn||'';newSn=JSON.parse(op.new_data||'{}').node_sn||''}catch{}
   return '<li><span>'+esc(op.action)+' · <code>'+esc(oldSn||'—')+'</code> → <code>'+esc(newSn||'—')+'</code></span><small>操作账号：'+esc(op.operator_username||'未知')+' · 实际操作人：'+esc(op.responsible)+' · '+esc(op.operated_at)+'</small></li>'
  }).join('');
  let opText=ops.length?'<details class="u-history"><summary>操作记录（'+ops.length+'）</summary><ol>'+history+'</ol></details>':'';
  let content=x?'<div class="u-component"><strong>'+esc(x.kp_name)+'</strong><span><b>NODE_SN</b> <code>'+esc(x.node_sn)+'</code></span><span><b>BMC_MAC</b> <code>'+esc(x.bmc_mac||'—')+'</code></span></div>':'<span class="u-empty-label">空位</span>';
  return '<div class="u-slot '+(x?'filled':'empty')+'"><span class="u-number">U'+u+'</span><div class="u-main">'+content+opText+'</div>'+(canEdit&&x?'<button class="secondary u-edit" data-server="'+server.id+'" data-component="'+x.id+'" data-u="'+u+'">编辑 / 更换 SN</button>':'')+'</div>'
 }).join('');
 const emptyNotice=components.length===0?'<div class="component-empty-notice">该 SN 还没有 46U 部件数据，46 个 U 位显示为空位属正常。 <button type="button" class="linklike" onclick="goImportServerComponents()">去导入部件明细</button></div>':'';
 return '<section class="component-device"><div class="component-device-title"><div><strong>GB300 · '+esc(server.sn)+'</strong><span>'+esc(server.room)+' / '+esc(server.rack)+'</span></div><span>'+components.length+' / 46 U</span></div>'+emptyNotice+'<div class="u-stack">'+slots+'</div></section>'
}
function goImportServerComponents(){
  const tab=document.querySelector('[data-tab=servers]');if(tab&&typeof activateTab==='function')activateTab(tab);
  const card=$('#serverComponentImportCard');
  if(card){card.classList.remove('hidden');try{card.scrollIntoView({behavior:'smooth',block:'start'})}catch(e){}}
  const dialog=$('#rackDialog');if(dialog&&typeof dialog.close==='function')dialog.close();
}

async function openRack(room,rack){let rows=(window.allServers||[]).filter(x=>x.room===room&&x.rack===rack);$('#rackDialog').dataset.room=room;$('#rackDialog').dataset.rack=rack;$('#rackDialogTitle').textContent=room+' · '+rack+' · 46U';$('#rackDialogSummary').textContent='GB300 服务器 · '+rows.length+' 台';$('#rackDialogBody').innerHTML='<p class="hint">正在读取设备信息…</p>';$('#rackDialog').showModal();try{let detail=await Promise.all(rows.map(x=>api('/api/servers/'+x.id+'/components')));$('#rackDialogBody').innerHTML=detail.map(x=>renderServerComponentStack(x.server,x.components,x.operations)).join('');$('#rackDialogBody').querySelectorAll('.u-edit').forEach(btn=>btn.onclick=()=>{let d=detail.find(x=>String(x.server.id)===btn.dataset.server),component=d.components.find(x=>String(x.id)===btn.dataset.component);openComponentEditor(d.server,component,Number(btn.dataset.u))})}catch(e){$('#rackDialogBody').innerHTML='<p class="import-errors">设备信息读取失败：'+esc(e.message)+'</p>'}}
function openComponentEditor(server,component,u){let f=$('#componentEditForm');f.reset();f.dataset.componentId=component.id;f.elements.node_sn.value=component.node_sn;f.elements.responsible.value=me?.username||'';$('#componentEditU').value='U'+component.node_u;$('#componentEditKp').value=component.kp_name;$('#componentEditTitle').textContent='编辑 / 更换 U'+u+' 的 NODE_SN';$('#componentEditAction').value='部件更换';$('#componentEditMsg').textContent='更换会把原 NODE_SN 自动登记为服务器备件；信息修正仅用于更正录入错误。U 位、KP_NAME 和 BMC_MAC 固定不变。';$('#componentEditDialog').showModal()}
$('#componentEditForm').onsubmit=async e=>{e.preventDefault();let f=e.target,payload={node_sn:f.elements.node_sn.value.trim(),responsible:f.elements.responsible.value.trim(),action:$('#componentEditAction').value};try{await api('/api/server-components/'+f.dataset.componentId,{method:'PATCH',body:JSON.stringify(payload)});$('#componentEditDialog').close();await openRack($('#rackDialog').dataset.room,$('#rackDialog').dataset.rack)}catch(err){$('#componentEditMsg').textContent=err.message}}
$('#serverSearch').oninput=renderServerMap;$('#serverBackButton').onclick=()=>{selectedServerRoom=null;renderServerMap()};$('#rackDialog').onclick=e=>{if(e.target===$('#rackDialog'))$('#rackDialog').close()};
start();


/* ===== 物料台账改造：字段映射编辑 / 分页预览 / 行内编辑 / 删除二次确认 ===== */
const IMPORT_PREVIEW_FIELDS=['category','model','description','planned','supplier','location','owner','notes'];
const IMPORT_PAGE_SIZE=50;
window.importPage=0;window.currentImportData=null;window.itemImportPreviewRows=[];

async function api(url,opts={}){
  let _busyBtn=null,_busyBody=false;
  if(typeof document!=='undefined'){
    const el=document.activeElement;
    if(el&&el.tagName==='BUTTON'&&!el.disabled){_busyBtn=el;el.disabled=true}
    if(document.body&&document.body.setAttribute){document.body.setAttribute('aria-busy','1');_busyBody=true}
  }
  opts.headers={'Content-Type':'application/json',...(opts.headers||{})};
  if(csrf&&opts.method&&opts.method!=='GET')opts.headers['X-CSRF-Token']=csrf;
  // 幂等键代表「同一个动作」：结果未确定时（网络异常/5xx）必须保留，
  // 下一次相同提交继续复用；只有明确成功或业务 4xx 才释放。
  let _idemSig=null,_settled=false;
  if(opts.method&&opts.method!=='GET'&&!opts.headers['X-Idempotency-Key']){
    const sig=(opts.method||'POST')+' '+url+' '+(opts.body||'');
    window._idemPending=window._idemPending||{};
    let prev=window._idemPending[sig];
    if(prev&&Date.now()-(prev.at||0)>300000)prev=null; // 5 分钟后视为过期
    const k=(prev&&prev.pending)?prev.k:((window.crypto&&crypto.randomUUID)?crypto.randomUUID():('wb-'+Date.now()+'-'+Math.random().toString(36).slice(2)));
    window._idemPending[sig]={k,pending:true,at:Date.now()};
    opts.headers['X-Idempotency-Key']=k;
    _idemSig=sig;
  }
  try{
    let r=await fetch(url,opts),d=await r.json().catch(()=>({}));
    if(!r.ok){
      if(r.status>=400&&r.status<500)_settled=true; // 业务已明确拒绝
      let e=Error(d.error||'请求失败');e.data=d;e.status=r.status;throw e
    }
    _settled=true;
    return d;
  }finally{
    if(_busyBtn)_busyBtn.disabled=false;
    if(_busyBody&&document.body.removeAttribute)document.body.removeAttribute('aria-busy');
    if(_idemSig&&window._idemPending[_idemSig]){
      if(_settled)delete window._idemPending[_idemSig];
      else window._idemPending[_idemSig].pending=true; // 结果未知：保留键供重试
    }
  }
}

function previewCell(value,row,field,type='text',readOnly=false){
  return `<input class="preview-edit" data-row="${row}" data-field="${field}" type="${type}" value="${esc(value??'')}"${readOnly?' readonly title="更新模式下型号是匹配键，不可修改"':''} aria-label="第${row+1}行 ${esc(importFieldNames[field]||field)}">`;
}

function collectPreviewRows(container,source,fields,numberFields=[]){
  return source.map((row,i)=>{
    let copy={...row};
    for(let field of fields){
      let input=container.querySelector(`[data-row="${i}"][data-field="${field}"]`);
      if(!input)continue;
      if(numberFields.includes(field))copy[field]=input.value.trim()===''?'':Number(input.value);
      else copy[field]=input.value.trim();
    }
    return copy;
  });
}

function collectVisiblePreview(){
  const box=$('#importPreview');if(!box)return;
  const rows=window.itemImportPreviewRows||[];
  const start=window.importPage*IMPORT_PAGE_SIZE;
  rows.slice(start,start+IMPORT_PAGE_SIZE).forEach((row,i)=>{
    IMPORT_PREVIEW_FIELDS.forEach(f=>{
      const el=box.querySelector(`[data-row="${i}"][data-field="${f}"]`);
      if(!el||el.readOnly)return;
      row[f]=(f==='planned')?(el.value.trim()===''?'':(Number.isFinite(Number(el.value))?Number(el.value):el.value)):el.value.trim();
    });
  });
}

function renderImportPage(){
  const d=window.currentImportData||{};
  const rows=window.itemImportPreviewRows||[];
  const start=window.importPage*IMPORT_PAGE_SIZE;
  const page=rows.slice(start,start+IMPORT_PAGE_SIZE);
  const lockModel=(d.mode||'create')==='update';
  const html=page.map((x,i)=>`<tr><td>${previewCell(x.category,i,'category')}</td><td>${previewCell(x.model,i,'model','text',lockModel)}</td><td>${previewCell(x.description,i,'description')}</td><td>${previewCell(x.planned,i,'planned','number')}</td><td>${previewCell(x.supplier,i,'supplier')}</td><td>${previewCell(x.location,i,'location')}</td><td>${previewCell(x.owner,i,'owner')}</td><td>${previewCell(x.notes,i,'notes')}</td><td><button type="button" class="secondary" onclick="pvDelete('items',null,${i})">删除</button></td></tr>`).join('');
  const body=$('#importPreviewBody');
  if(body)body.innerHTML=html||'<tr><td colspan="9" class="hint">没有可预览的数据行；可点「恢复已删除的行」</td></tr>';
  const total=Math.max(1,Math.ceil(rows.length/IMPORT_PAGE_SIZE));
  const info=$('#importPageInfo');
  if(info)info.textContent=`第 ${rows.length?start+1:0}–${Math.min(start+IMPORT_PAGE_SIZE,rows.length)} 条 / 共 ${rows.length} 条 · 第 ${window.importPage+1}/${total} 页`;
  const prev=$('#importPrevPage'),next=$('#importNextPage');
  if(prev)prev.disabled=window.importPage<=0;
  if(next)next.disabled=window.importPage>=total-1;
}

function switchImportPage(delta){
  collectVisiblePreview();
  const total=Math.max(1,Math.ceil((window.itemImportPreviewRows||[]).length/IMPORT_PAGE_SIZE));
  window.importPage=Math.min(total-1,Math.max(0,window.importPage+delta));
  renderImportPage();
}

function renderImportPreview(d){
  window.currentImportData=d;
  window.itemImportPreviewRows=d.preview_rows||d.samples||[];
  window.importPage=0;window.pvDeleted_items=[];
  const mode=d.mode||'create';
  const idx=d.mapping_index||{},reverse={};
  Object.entries(idx).forEach(([f,i])=>{reverse[i]=f});
  const headerOpts=(d.header_candidates||[]).map(x=>`<option value="${x.row}"${x.row===d.header_row?' selected':''}>第 ${x.row} 行：${esc((Array.isArray(x.cells)?x.cells:[]).filter(v=>String(v).trim()).slice(0,6).join(' | ')||'（空行）')}</option>`).join('');
  const colIdxs=d.active_columns||(d.columns||[]).map((_,i)=>i);
  const colHtml=colIdxs.map(i=>{
    const name=(d.columns||[])[i]||'';
    const opts=['<option value="">— 忽略此列 —</option>'].concat(
      IMPORT_PREVIEW_FIELDS.map(f=>`<option value="${f}"${reverse[i]===f?' selected':''}>${esc(importFieldNames[f]||f)}</option>`)).join('');
    return `<div class="map-col"><label>第 ${i+1} 列 · ${esc(String(name||'（无表头）').slice(0,40))}</label><select data-col="${i}">${opts}</select></div>`;
  }).join('');
  const errors=(d.errors||[]).map(x=>`<li>${esc(x)}</li>`).join('');
  const warnings=(d.warnings||[]).map(x=>`<li>${esc(x)}</li>`).join('');
  const mapping=Object.entries(d.mapping||{}).map(([k,v])=>`<span class="badge">${esc(importFieldNames[k]||k)} ← ${esc(v)}</span>`).join(' ');
  const verb=mode==='update'?'确认更新':'确认导入';
  $('#importMsg').textContent=`已选第 ${d.header_row} 行为表头，共 ${d.rows} 条记录。${d.can_import?'校验通过，可'+(mode==='update'?'更新':'导入')+'。':'请调整下面的表头行或字段对应后重新校验。'}`;
  $('#importPreview').innerHTML=`<div class="card">
    <div class="import-section"><b>① 表头所在行</b>
      <div class="map-row"><select id="importHeaderRow">${headerOpts||`<option value="${d.header_row}">第 ${d.header_row} 行</option>`}</select><button type="button" class="secondary" onclick="applyImportMapping()">应用修改并重新校验</button></div>
      <p class="hint">自动识别会扫描前 30 个非空行。表头不在第一行、或识别错了，直接在这里选。</p></div>
    <div class="import-section"><b>② 字段对应（每列选一个字段，已隐藏 ${d.hidden_columns||0} 个空列）</b>
      <div class="import-mapping">${mapping||'<span class="hint">暂未识别到字段，请手动指定</span>'}</div>
      <details class="mapping-details"${d.can_import&&Object.keys(d.mapping||{}).length?'':' open'}><summary>点击调整字段对应（逐列指定 / 重新校验）</summary><div id="importMapping" class="import-mapping-grid">${colHtml}</div></details>
      <p class="hint">${mode==='create'?'新增模式必需：型号、名称/规格；计划数量可留空（预览补填，默认 0）；类别可用整批默认值。':'更新模式必需：型号（作为匹配键，不可修改）；未提供的列保留台账现值。'}</p></div>
    ${d.suggest_import==='movements'?`<p class="import-errors"><b>可能选错导入类型</b>：这份表含库存变动特征列。若要登记入库，请到「库存变动明细」页面重新上传同一个 Excel；当前物料台账导入不会写入这些数量。</p>`:''}
    ${warnings?`<p><b>提示</b><ul>${warnings}</ul></p>`:''}
    ${errors?`<p class="import-errors"><b>错误（整批不会导入）</b><ul>${errors}</ul></p>`:''}
    <div class="import-section"><b>③ 预览数据（可直接修改）</b>
      <div class="import-pager"><button type="button" class="secondary" id="importPrevPage" onclick="switchImportPage(-1)">上一页</button><span id="importPageInfo"></span><button type="button" class="secondary" id="importNextPage" onclick="switchImportPage(1)">下一页</button></div>
      <div class="import-pager"><button type="button" class="secondary" id="itemDeletedRestore" onclick="pvRestore('items',null)">恢复已删除的行</button><span id="itemDeletedInfo" class="hint"></span></div>
      <p class="hint">不需要的行可以逐行删除；确认时只会写入当前预览中的行。</p>
      <div class="tablewrap preview-table"><table><thead><tr><th>类别</th><th>型号</th><th>名称/规格</th><th>计划数量</th><th>供应商/品牌</th><th>位置</th><th>负责人</th><th>备注</th><th>操作</th></tr></thead><tbody id="importPreviewBody"></tbody></table></div></div>
    ${(d.token||d.edit_token)?`<div class="import-section import-confirm-bar"><div class="confirm-bar-meta" id="itemConfirmMeta"></div><button type="button" id="confirmImport">${verb} ${d.rows} 条</button><div id="importResult" class="status" aria-live="polite"></div></div>`:''}
  </div>`;
  renderImportPage();pvUpdateBar('items',null);
  if($('#importCard')&&$('#importCard').tagName==='DETAILS')$('#importCard').open=true;
  if(d.token||d.edit_token)$('#confirmImport').onclick=()=>confirmImport(d.token||d.edit_token);
}

async function applyImportMapping(){
  const d=window.currentImportData;
  if(!d||!d.edit_token){$('#importMsg').textContent='预览已失效，请重新上传 Excel 文件';return}
  const mapping_index={},used={};let conflict=null;
  document.querySelectorAll('#importMapping [data-col]').forEach(sel=>{
    const f=sel.value;if(!f)return;
    if(used[f]!==undefined&&!conflict)conflict=importFieldNames[f]||f;
    used[f]=Number(sel.dataset.col);mapping_index[f]=Number(sel.dataset.col);
  });
  if(conflict){uiToast(`字段「${conflict}」被指定了多列，请给每个字段只留一列。`);return}
  if(!await uiConfirm('重新校验会按原始 Excel 重新抽取数据，之前在预览表格里手改的内容需要重填。继续吗？'))return;
  const header_row=Number($('#importHeaderRow').value)||d.header_row;
  $('#importMsg').textContent='正在按新的表头行和字段对应重新校验…';
  try{
    const r=await api('/api/import-remap',{method:'POST',body:JSON.stringify({edit_token:d.edit_token,header_row,mapping_index})});
    renderImportPreview(r);
  }catch(e){$('#importMsg').textContent=e.message}
}

async function confirmImport(token){
  let _done=false;
  const _t0=typeof performance!=='undefined'&&performance.now?performance.now():Date.now(),_btn=pvBtn('#confirmImport');
  collectVisiblePreview();pvBegin(_btn,'校验中…');
  const d=window.currentImportData||{};
  const rows=window.itemImportPreviewRows||[];
  if(!rows.length){const m='预览中没有可导入的数据；可点「恢复已删除的行」或重新上传';uiToast(m);$('#importResult').textContent=m;return}
  const verb=d.mode==='update'?'按预览更新这些物料的台账字段（型号与库存数量不变）':'将预览中的物料写入物料台账';
  if(!await uiConfirm(`确认${verb}？本次不会写入库存变动明细。`))return;
  try{
    pvBegin(_btn,'写入中…');
    const r=await api('/api/import-items',{method:'POST',body:JSON.stringify({token,rows:window.itemImportPreviewRows})});
    const msg=(d.mode==='update'?`已更新 ${r.imported} 条物料台账。`:`已导入 ${r.imported} 条物料，库存变动明细未改动。`)+`（${r.imported} 条，耗时 ${pvSec(_t0).toFixed(1)} 秒）`;
    $('#importResult').textContent=msg;uiToast(msg,'ok');
    window.currentImportData=null;window.itemImportPreviewRows=[];window.pvDeleted_items=[];
    await loadItems();filterItems();
    _done=true;
  }catch(e){$('#importResult').textContent=e.message}
  finally{pvEnd(_btn,_done)}
}

$('#importForm').onsubmit=async e=>{
  e.preventDefault();
  const file=e.target.elements.file.files[0];if(!file)return;
  $('#importMsg').textContent='正在读取并检查表格…';$('#importPreview').innerHTML='';
  try{
    const r=await fetch('/api/import-preview',{method:'POST',headers:{
      'Content-Type':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      'X-CSRF-Token':csrf,
      'X-Category-Default':e.target.elements.default_category.value,
      'X-Import-Mode':e.target.elements.import_mode.value},body:file});
    const d=await r.json();if(!r.ok)throw Error(d.error||'无法读取表格');
    renderImportPreview(d);
  }catch(err){$('#importMsg').textContent=err.message}
};

const ITEM_COL_COUNT=14; // 与 index.html 台账表头列数一致，测试会校验
let itemSort={key:null,dir:1};
function sortItemsBy(key){
  const body=$('#itemsBody');
  if(body&&body.querySelector('[data-edit]')){
    uiToast('请先保存或取消正在编辑的行，再点击排序。');return;
  }
  if(itemSort.key===key)itemSort.dir=-itemSort.dir;
  else itemSort={key,dir:1};
  loadItems();
}
function bindItemSortHeaders(){
  const ths=document.querySelectorAll('#items thead th[data-sort-key]');
  ths.forEach(th=>{
    const key=th.dataset.sortKey;if(!key)return;
    th.style.cursor='pointer';th.setAttribute('tabindex','0');
    th.title='点击按'+({model:'型号',available:'可用库存',not_received:'未到数量'}[key])+'排序';
    th.setAttribute('aria-sort',itemSort.key===key?(itemSort.dir===1?'ascending':'descending'):'none');
    th.onclick=()=>sortItemsBy(key);
    th.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();sortItemsBy(key)}};
  });
}
async function loadItems(){
  let {items}=await api('/api/items');
  if(itemSort.key){const k=itemSort.key,dir=itemSort.dir;
    items=[...items].sort((a,b)=>{
      const av=a[k],bv=b[k];
      const cmp=(typeof av==='number'&&typeof bv==='number')?(av-bv):String(av).localeCompare(String(bv),'zh-CN');
      return cmp*dir;
    });
  }
  const canEdit=me.is_admin||me.can_edit;
  $('#itemsBody').innerHTML=items.map(x=>`<tr data-id="${x.id}" data-category="${esc(x.category)}"><td data-field="category">${esc(x.category)}</td><td data-field="model">${esc(x.model)}</td><td data-field="description">${esc(x.description)}</td><td data-field="supplier">${esc(x.supplier)}</td><td data-field="planned">${x.planned}</td><td>${x.received}</td><td>${x.used}</td><td>${x.damaged}</td><td><b>${x.available}</b></td><td>${x.not_received}</td><td data-field="location">${esc(x.location)}</td><td data-field="owner">${esc(x.owner)}</td><td data-field="notes">${esc(x.notes)}</td><td class="row-actions">${canEdit?`<button type="button" class="secondary" onclick="startEditItem(${x.id})">编辑</button>`:''}${me.is_admin?`<button type="button" class="danger" onclick="deleteItem(${x.id})">删除</button>`:''}</td></tr>`).join('');
  fillMovementModels(items);window.allItems=items;filterItems();
  const hint=$('#emptyLedgerHint');if(hint)hint.classList.toggle('hidden',items.length>0||!me.is_admin);
  bindItemSortHeaders();
}

function startEditItem(id){
  const tr=document.querySelector(`#itemsBody tr[data-id="${id}"]`);
  const it=(window.allItems||[]).find(x=>x.id===id);
  if(!tr||!it)return;
  ['category','model','description','supplier','planned','location','owner','notes'].forEach(f=>{
    const td=tr.querySelector(`[data-field="${f}"]`);if(!td)return;
    if(f==='category')td.innerHTML=`<select data-edit="${f}"><option${it.category==='光模块'?' selected':''}>光模块</option><option${it.category==='线缆'?' selected':''}>线缆</option></select>`;
    else if(f==='planned')td.innerHTML=`<input data-edit="${f}" type="number" min="0" value="${it.planned}">`;
    else td.innerHTML=`<input data-edit="${f}" value="${esc(it[f]||'')}">`;
  });
  tr.querySelector('.row-actions').innerHTML=`<button type="button" onclick="saveItemEdit(${id})">保存</button> <button type="button" class="secondary" onclick="loadItems()">取消</button>`;
}

async function saveItemEdit(id){
  const tr=document.querySelector(`#itemsBody tr[data-id="${id}"]`);if(!tr)return;
  const payload={};
  tr.querySelectorAll('[data-edit]').forEach(el=>{payload[el.dataset.edit]=el.value});
  try{
    await api('/api/items/'+id,{method:'PATCH',body:JSON.stringify(payload)});
    await loadItems();await loadMoves();
  }catch(e){uiToast(e.message);await loadItems()}
}

async function deleteItem(id){
  const it=(window.allItems||[]).find(x=>x.id===id);if(!it)return;
  if(!await uiConfirm(`确认删除物料「${it.model}」？删除前快照会写入回收站。`,'删除物料'))return;
  const attempt=confirmModel=>api('/api/items/'+id,{method:'DELETE',body:JSON.stringify(confirmModel?{confirm_model:confirmModel}:{})});
  try{
    await attempt('');
    await loadItems();await loadMoves();uiToast('物料已删除，可在回收站还原。','ok');return;
  }catch(e){
    const d=e.data||{};
    if(!d.requires_confirm){uiToast(e.message);return}
    const c=d.counts||{};
    const msg=`该物料已有 ${c.movements||0} 条库存变动明细、${c.spares||0} 条坏件 SN 记录、${c.cases||0} 条备件单。\n\n删除会一并抹掉这些历史（系统只保留一份删除前快照供追溯），界面上不可恢复。\n\n如确需删除，请输入完整型号：${d.model}`;
    const typed=await uiPrompt(msg,d.model);
    if(typed===null)return;
    try{
      await attempt(typed.trim());
      await loadItems();await loadMoves();uiToast('已删除；删除前快照已写入审计记录。','ok');
    }catch(e2){uiToast(e2.message)}
  }
}



/* ===== 页面内确认/输入/提示：替代被内置浏览器拦截的原生弹窗 ===== */
function uiModal(opts){
  return new Promise(resolve=>{
    const priorFocus=(typeof document!=='undefined'&&document.activeElement)?document.activeElement:null;
    const wrap=document.createElement('div');
    wrap.className='ui-modal-backdrop';
    wrap.setAttribute('role','dialog');wrap.setAttribute('aria-modal','true');
    wrap.setAttribute('aria-label',opts.title||'请确认');
    wrap.innerHTML=`<div class="ui-modal"><h3>${esc(opts.title||'请确认')}</h3><p class="ui-modal-msg">${esc(opts.message||'')}</p>${opts.input?`<input id="uiModalInput" autocomplete="off" placeholder="${esc(opts.placeholder||'')}">`:''}<div class="ui-modal-error"></div><div class="ui-modal-actions"><button type="button" class="secondary" data-act="cancel">取消</button><button type="button"${opts.danger?' class="danger"':''} data-act="ok">${esc(opts.okText||'确定')}</button></div></div>`;
    const done=v=>{document.removeEventListener('keydown',onKey);wrap.remove();
      if(priorFocus&&priorFocus.focus)try{priorFocus.focus()}catch(e){} resolve(v)};
    const submit=()=>{
      let val=true;
      if(opts.input){
        val=(wrap.querySelector('#uiModalInput')?.value||'').trim();
        if(!val){wrap.querySelector('.ui-modal-error').textContent='请输入内容';return}
        if(opts.expected&&val!==opts.expected){
          wrap.querySelector('.ui-modal-error').textContent=`输入不匹配，请完整输入「${opts.expected}」`;return;
        }
      }
      done(val);
    };
    const onKey=e=>{
      if(e.key==='Escape')done(opts.input?null:false);
      if(e.key==='Enter'&&opts.input)submit();
    };
    document.addEventListener('keydown',onKey);
    wrap.addEventListener('click',e=>{
      const act=e.target?.dataset?.act;
      if(act==='cancel'||e.target===wrap)done(opts.input?null:false);
      else if(act==='ok')submit();
    });
    document.body.appendChild(wrap);
    if(opts.input)wrap.querySelector('#uiModalInput')?.focus();
    else wrap.querySelector('[data-act="ok"]')?.focus();
  });
}
function uiConfirm(message,title='请确认'){return uiModal({title,message,okText:'确定'}).then(v=>v===true)}
function uiPrompt(message,expected,title='二次确认'){return uiModal({title,message,input:true,expected,placeholder:'完整输入：'+expected,okText:'确认删除',danger:true})}
function uiToast(msg,type='err'){
  let t=document.getElementById('uiToast');
  if(!t){t=document.createElement('div');t.id='uiToast';t.setAttribute('role','status');
    t.setAttribute('aria-live','polite');document.body.appendChild(t)}
  t.className='ui-toast '+(type==='ok'?'ok':'err');t.textContent=String(msg||'');
  clearTimeout(window._uiToastTimer);
  window._uiToastTimer=setTimeout(()=>t.remove(),5000);
}



/* ===== 回收站 / 操作日志 ===== */
async function loadRecycle(){
  try{
    const d=await api('/api/recycle');
    window.recycleRecords=d.records||[];
    $('#recycleBody').innerHTML=window.recycleRecords.map(r=>`<tr>
      <td>${esc(r.deleted_at||'')}</td><td>${esc(r.operator||'—')}</td><td>${esc(r.category||'')}</td>
      <td>${esc(r.model||'')}</td><td>${esc(r.description||'')}</td>
      <td>${r.movement_count||0}</td><td>${r.spare_count||0}</td>
      <td>${r.restored_at?'<span class="badge">已还原</span>':(r.can_restore?'<span class="badge">可还原</span>':`<span class="import-errors">${esc(r.reason)}</span>`)}</td>
      <td class="row-actions"><button type="button" class="secondary" data-act="detail" data-id="${r.id}">详情</button>${r.can_restore?`<button type="button" data-act="restore" data-id="${r.id}">还原</button>`:''}<button type="button" class="danger" data-act="purge" data-id="${r.id}">彻底删除</button></td></tr>`).join('')||'<tr><td colspan="9" class="hint">回收站为空</td></tr>';
    $('#recycle').onclick=e=>{
      const btn=e.target&&e.target.closest?e.target.closest('[data-act]'):null;
      if(!btn)return;
      const act=btn.dataset.act,rid=Number(btn.dataset.id);
      if(act==='detail')return showRecycleDetail(rid);
      if(act==='restore')return restoreRecycle([rid]);
      if(act==='purge')return purgeRecycle([rid]);
      if(act==='refresh')return loadRecycle();
      if(act==='restore-all')return restoreAllRecycle();
      if(act==='purge-all')return purgeAllRecycle();
    };
    $('#operationLogBody').innerHTML=(d.logs||[]).map(x=>`<tr><td>${esc(x.operated_at||'')}</td><td>${esc(x.operator||'—')}</td><td>${esc(x.action||'')}</td><td>${esc(x.target||'')}</td><td>${esc(x.detail||'')}</td></tr>`).join('')||'<tr><td colspan="5" class="hint">暂无操作记录</td></tr>';
    const pending=window.recycleRecords.filter(x=>x.can_restore).length;
    $('#recycleMsg').textContent=`共 ${window.recycleRecords.length} 条删除记录，其中 ${pending} 条可还原。`;
  }catch(e){uiToast(e.message)}
}

function renderRecycleDetail(rec){
  const s=rec.snapshot||{},it=s.item||{};
  const fields=[['类别',it.category],['型号',it.model],['名称/规格',it.description],['供应商/品牌',it.supplier],
    ['计划数量',it.planned],['存放位置',it.location],['负责人',it.owner],['备注',it.notes]]
    .map(([k,v])=>`<tr><th>${esc(k)}</th><td>${esc(v??'')}</td></tr>`).join('');
  const mv=(s.movements||[]).map(x=>`<tr><td>${esc(x.moved_at||'')}</td><td>${esc(x.kind||'')}</td><td>${x.qty}</td><td>${esc(x.location||'')}</td><td>${esc(x.owner||'')}</td><td>${esc(x.notes||'')}</td></tr>`).join('');
  const sp=(s.material_spares||[]).map(x=>`<tr><td>${esc(x.serial_number||'（未补录）')}</td><td>${esc(x.status||'')}</td><td>${esc(x.responsible||'')}</td></tr>`).join('');
  const el=$('#recycleDetail');
  if(!el)return;
  el.innerHTML=`<div class="card"><div class="toolbar"><h3>删除前快照 · ${esc(it.model||'')}</h3><button type="button" class="secondary" data-act="close">收起</button></div>
    <div class="grid"><div><table><tbody>${fields}</tbody></table></div><div>
      <b>库存变动明细（${(s.movements||[]).length}）</b><div class="tablewrap"><table><thead><tr><th>日期</th><th>类型</th><th>数量</th><th>位置</th><th>负责人</th><th>备注</th></tr></thead><tbody>${mv||'<tr><td colspan="6" class="hint">无</td></tr>'}</tbody></table></div>
      <b>坏件 SN（${(s.material_spares||[]).length}）</b><div class="tablewrap"><table><thead><tr><th>SN</th><th>状态</th><th>责任人</th></tr></thead><tbody>${sp||'<tr><td colspan="3" class="hint">无</td></tr>'}</tbody></table></div>
    </div></div></div>`;
  el.onclick=e=>{if(e.target&&e.target.dataset&&e.target.dataset.act==='close')closeRecycleDetail()};
  if(el.scrollIntoView)el.scrollIntoView({behavior:'smooth',block:'nearest'});
  const msg=$('#recycleMsg');
  if(msg)msg.textContent=`已展开删除前快照：${it.model||''}（${(s.movements||[]).length} 条明细 / ${(s.material_spares||[]).length} 条坏件）。点「收起」关闭。`;
}

function showRecycleDetail(id){
  try{
    const rec=(window.recycleRecords||[]).find(x=>x.id===id);
    if(rec&&rec.snapshot){renderRecycleDetail(rec);return}
    api('/api/recycle/'+id).then(d=>renderRecycleDetail({snapshot:d.snapshot||{}}))
      .catch(e=>{$('#recycleMsg').textContent='详情加载失败：'+e.message;uiToast(e.message)});
  }catch(e){
    $('#recycleMsg').textContent='详情加载失败：'+e.message;uiToast(e.message);
  }
}
function closeRecycleDetail(){const el=$('#recycleDetail');if(el)el.innerHTML=''}

async function restoreRecycle(ids){
  const ok=await uiConfirm(ids.length===1?'确认还原这条物料及其全部历史记录？':`确认还原选中的 ${ids.length} 条物料及其全部历史记录？`);
  if(!ok)return;
  try{
    const r=await api('/api/recycle/restore',{method:'POST',body:JSON.stringify({ids})});
    let msg=`已还原 ${r.restored} 条物料。`;
    if(r.skipped&&r.skipped.length)msg+=` 跳过 ${r.skipped.length} 条：`+r.skipped.slice(0,3).map(x=>`#${x.id} ${x.reason}`).join('；');
    $('#recycleMsg').textContent=msg;uiToast(msg,'ok');closeRecycleDetail();
    await loadRecycle();await loadItems();await loadMoves();
  }catch(e){$('#recycleMsg').textContent='还原失败：'+e.message;uiToast(e.message)}
}
async function restoreAllRecycle(){
  const ids=(window.recycleRecords||[]).filter(x=>x.can_restore).map(x=>x.id);
  if(!ids.length){uiToast('没有可还原的记录');return}
  return restoreRecycle(ids);
}



/* ===== 回收站彻底删除（不可逆） ===== */
async function purgeRecycle(ids){
  const recs=ids.map(id=>(window.recycleRecords||[]).find(x=>x.id===id)).filter(Boolean);
  const names=recs.slice(0,3).map(x=>x.model||('#'+x.id)).join('、');
  const msg=ids.length===1
    ? `确认彻底删除「${names}」的删除前快照吗？\n\n此操作会永久移除回收站记录及其中的库存变动明细、坏件 SN 历史，之后无法再还原，且不可撤销。操作会记入操作日志。`
    : `确认彻底删除 ${ids.length} 条回收站记录（${names}${ids.length>3?' 等':''}）吗？\n\n这些删除前快照会被永久移除，之后无法再还原，且不可撤销。操作会记入操作日志。`;
  const ok=await uiConfirm(msg,'彻底删除（不可恢复）');
  if(!ok)return;
  try{
    const r=await api('/api/recycle/purge',{method:'POST',body:JSON.stringify({ids})});
    const m=`已彻底删除 ${r.purged} 条记录。`+(r.skipped&&r.skipped.length?` 跳过 ${r.skipped.length} 条。`:'');
    $('#recycleMsg').textContent=m;uiToast(m,'ok');closeRecycleDetail();
    await loadRecycle();
  }catch(e){$('#recycleMsg').textContent='彻底删除失败：'+e.message;uiToast(e.message)}
}
async function purgeAllRecycle(){
  const ids=(window.recycleRecords||[]).map(x=>x.id);
  if(!ids.length){uiToast('回收站为空');return}
  return purgeRecycle(ids);
}



/* ===== 入库明细导入：字段映射编辑 / 表头行 / 分页预览 ===== */
const MOVEMENT_PREVIEW_FIELDS=['model','qty','date','category','kind','location','owner','notes'];
const MOVEMENT_EDITABLE=['moved_at','qty','location','owner','notes'];
const MOVEMENT_PAGE_SIZE=50;
window.mvImportPage=0;window.mvImportData=null;window.movementImportPreviewRows=[];
window.movementImportToken=null;

function mvCell(value,row,field,type='text',readOnly=false){
  return `<input class="preview-edit" data-row="${row}" data-field="${field}" type="${type}" value="${esc(value??'')}"${readOnly?' readonly':''} aria-label="第${row+1}行 ${esc(movementFieldNames[field]||field)}">`;
}

function collectMovementVisible(){
  const box=$('#movementImportPreview');if(!box)return;
  const rows=window.movementImportPreviewRows||[];
  const start=window.mvImportPage*MOVEMENT_PAGE_SIZE;
  rows.slice(start,start+MOVEMENT_PAGE_SIZE).forEach((row,i)=>{
    MOVEMENT_EDITABLE.forEach(f=>{
      const el=box.querySelector(`[data-row="${i}"][data-field="${f}"]`);
      if(!el||el.readOnly)return;
      row[f]=(f==='qty')?(el.value.trim()===''?'':(Number.isFinite(Number(el.value))?Number(el.value):el.value)):el.value.trim();
    });
  });
  pvUpdateBar('movements',null);
}

function renderMovementPage(){
  const rows=window.movementImportPreviewRows||[];
  const start=window.mvImportPage*MOVEMENT_PAGE_SIZE;
  const page=rows.slice(start,start+MOVEMENT_PAGE_SIZE);
  $('#movementPreviewBody').innerHTML=page.map((x,i)=>`<tr>
    <td>${mvCell(x.moved_at,i,'moved_at','date')}</td><td>入库</td>
    <td>${mvCell(x.category,i,'category','text',true)}</td>
    <td>${mvCell(x.model,i,'model','text',true)}</td>
    <td>${mvCell(x.qty,i,'qty','number')}</td>
    <td>${mvCell(x.location,i,'location')}</td>
    <td>${mvCell(x.owner,i,'owner')}</td>
    <td>${mvCell(x.notes,i,'notes')}</td>
    <td><button type="button" class="secondary" data-act="mv-del" data-idx="${i}">删除</button></td></tr>`).join('')||'<tr><td colspan="9" class="hint">没有可预览的数据行；可点「恢复已删除的行」</td></tr>';
  const total=Math.max(1,Math.ceil(rows.length/MOVEMENT_PAGE_SIZE));
  $('#movementPageInfo').textContent=`第 ${rows.length?start+1:0}–${Math.min(start+MOVEMENT_PAGE_SIZE,rows.length)} 条 / 共 ${rows.length} 条 · 第 ${window.mvImportPage+1}/${total} 页`;
  $('#mvPrevPage').disabled=window.mvImportPage<=0;
  $('#mvNextPage').disabled=window.mvImportPage>=total-1;
}

function switchMovementPage(delta){
  collectMovementVisible();
  const total=Math.max(1,Math.ceil((window.movementImportPreviewRows||[]).length/MOVEMENT_PAGE_SIZE));
  window.mvImportPage=Math.min(total-1,Math.max(0,window.mvImportPage+delta));
  renderMovementPage();
}

function renderMovementImportPreview(d){
  window.mvImportData=d;window.movementImportPreviewRows=d.preview_rows||d.samples||[];
  window.mvImportPage=0;window.movementImportToken=d.token||d.edit_token||null;
  window.pvDeleted_movements=[];
  const idx=d.mapping_index||{},reverse={};
  Object.entries(idx).forEach(([f,i])=>{reverse[i]=f});
  const headerOpts=(d.header_candidates||[]).map(x=>`<option value="${x.row}"${x.row===d.header_row?' selected':''}>第 ${x.row} 行：${esc((Array.isArray(x.cells)?x.cells:[]).filter(v=>String(v).trim()).slice(0,6).join(' | ')||'（空行）')}</option>`).join('');
  const colIdxs=d.active_columns||(d.columns||[]).map((_,i)=>i);
  const colHtml=colIdxs.map(i=>{
    const name=(d.columns||[])[i]||'';
    const opts=['<option value="">— 忽略此列 —</option>'].concat(
      MOVEMENT_PREVIEW_FIELDS.map(f=>`<option value="${f}"${reverse[i]===f?' selected':''}>${esc(movementFieldNames[f]||f)}</option>`)).join('');
    return `<div class="map-col"><label>第 ${i+1} 列 · ${esc(String(name||'（无表头）').slice(0,40))}</label><select data-col="${i}">${opts}</select></div>`;
  }).join('');
  const mapping=Object.entries(d.mapping||{}).map(([k,v])=>`<span class="badge">${esc(movementFieldNames[k]||k)} ← ${esc(v)}</span>`).join(' ');
  const errors=(d.errors||[]).map(x=>`<li>${esc(x)}</li>`).join('');
  const warnings=(d.warnings||[]).map(x=>`<li>${esc(x)}</li>`).join('');
  $('#movementImportMsg').textContent=`已选第 ${d.header_row} 行为表头，共 ${d.rows} 条记录。${d.can_import?'可确认导入；如表格未提供数量，请在下方补填或删除不需要的行。':'请调整下面的表头行或字段对应后重新校验。'}`;
  $('#movementImportPreview').innerHTML=`<div class="card">
    <div class="import-section"><b>① 表头所在行</b>
      <div class="map-row"><select id="movementHeaderRow">${headerOpts||`<option value="${d.header_row}">第 ${d.header_row} 行</option>`}</select><button type="button" class="secondary" data-act="mv-apply">应用修改并重新校验</button></div>
      <p class="hint">自动识别扫描前 30 个非空行；猜错了直接在这里改。</p></div>
    <div class="import-section"><b>② 字段对应（每列选一个字段，已隐藏 ${d.hidden_columns||0} 个空列）</b>
      <div class="import-mapping">${mapping||'<span class="hint">暂未识别到字段，请手动指定</span>'}</div>
      <details class="mapping-details"${d.can_import&&Object.keys(d.mapping||{}).length?'':' open'}><summary>点击调整字段对应（逐列指定 / 重新校验）</summary><div id="movementMapping" class="import-mapping-grid">${colHtml}</div></details>
      <p class="hint">必需字段只有「型号」和「入库数量」；类别自动按型号从台账带出，其他列选填。数量列名不必叫「入库数量」，在这里指定即可。</p></div>
    ${warnings?`<p><b>提示</b><ul>${warnings}</ul></p>`:''}
    ${errors?`<p class="import-errors"><b>错误（整批不会导入）</b><ul>${errors}</ul></p>`:''}
    <div class="import-section"><b>③ 预览数据（日期、数量、位置、负责人、备注可修改）</b>
      <div class="import-pager"><button type="button" class="secondary" id="mvPrevPage" data-act="mv-prev">上一页</button><span id="movementPageInfo"></span><button type="button" class="secondary" id="mvNextPage" data-act="mv-next">下一页</button></div>
      <div class="import-pager"><button type="button" class="secondary" id="mvDeletedRestore" data-act="mv-restore">恢复已删除的行</button><span id="mvDeletedInfo" class="hint"></span><span id="mvQtyInfo" class="hint"></span></div>
      <p class="hint">不需要的行可以逐行删除；删错了点「恢复已删除的行」。确认时只会写入当前预览中的行。</p>
      <div class="tablewrap preview-table"><table><thead><tr><th>日期</th><th>业务类型</th><th>类别</th><th>型号</th><th>数量</th><th>位置/去向</th><th>负责人</th><th>备注</th><th>操作</th></tr></thead><tbody id="movementPreviewBody"></tbody></table></div></div>
    ${(d.token||d.edit_token)?`<div class="import-section import-confirm-bar"><div class="confirm-bar-meta" id="mvConfirmMeta"></div><button type="button" id="movementConfirmBtn" data-act="mv-confirm">确认导入 ${d.rows} 条入库明细</button><div id="movementImportResult" class="status" aria-live="polite"></div></div>`:''}
  </div>`;
  $('#movementImportPreview').onclick=e=>{
    const b=e.target&&e.target.closest?e.target.closest('[data-act]'):null;
    if(!b)return;
    const act=b.dataset.act;
    if(act==='mv-apply')return applyMovementMapping();
    if(act==='mv-prev')return switchMovementPage(-1);
    if(act==='mv-next')return switchMovementPage(1);
    if(act==='mv-del')return pvDelete('movements',null,b.dataset.idx);
    if(act==='mv-restore')return pvRestore('movements',null);
    if(act==='mv-confirm')return confirmMovementImport(window.movementImportToken);
  };
  renderMovementPage();pvUpdateBar('movements',null);
}

async function applyMovementMapping(){
  const d=window.mvImportData;
  if(!d||!d.edit_token){$('#movementImportMsg').textContent='预览已失效，请重新上传 Excel 文件';return}
  const mapping_index={},used={};let conflict=null;
  document.querySelectorAll('#movementMapping [data-col]').forEach(sel=>{
    const f=sel.value;if(!f)return;
    if(used[f]!==undefined&&!conflict)conflict=movementFieldNames[f]||f;
    used[f]=Number(sel.dataset.col);mapping_index[f]=Number(sel.dataset.col);
  });
  if(conflict){uiToast(`字段「${conflict}」被指定了多列，请给每个字段只留一列。`);return}
  if(!await uiConfirm('重新校验会按原始 Excel 重新抽取数据，之前在预览表格里手改的内容需要重填。继续吗？'))return;
  const header_row=Number($('#movementHeaderRow').value)||d.header_row;
  $('#movementImportMsg').textContent='正在按新的表头行和字段对应重新校验…';
  try{
    const r=await api('/api/import-movements-remap',{method:'POST',body:JSON.stringify({edit_token:d.edit_token,header_row,mapping_index})});
    renderMovementImportPreview(r);
  }catch(e){$('#movementImportMsg').textContent=e.message}
}

async function confirmMovementImport(token){
  let _done=false;
  const _t0=typeof performance!=='undefined'&&performance.now?performance.now():Date.now(),_btn=pvBtn('#movementConfirmBtn');
  collectMovementVisible();pvBegin(_btn,'校验中…');
  if(!token){const m='预览已失效，请重新上传 Excel 文件';uiToast(m);$('#movementImportResult').textContent=m;return}
  const rows=window.movementImportPreviewRows||[];
  if(!rows.length){const m='预览中没有可导入的数据；可点「恢复已删除的行」或重新上传';uiToast(m);$('#movementImportResult').textContent=m;return}
  const bad=rows.findIndex(r=>!(Number(r.qty)>0));
  if(bad>=0){const m=`第 ${bad+1} 行入库数量必须大于 0，请在预览中补填，或删除该行后再导入。`;uiToast(m);$('#movementImportResult').textContent=m;return}
  if(!await uiConfirm(`确认将预览中的 ${rows.length} 条记录作为「入库」写入库存变动明细？已删除的行不会写入。`))return;
  try{
    pvBegin(_btn,'写入中…');
    const d=await api('/api/import-movements',{method:'POST',body:JSON.stringify({token,rows:window.movementImportPreviewRows})});
    const msg=`已导入 ${d.imported} 条入库明细，物料台账字段未修改。（${d.imported} 条，耗时 ${pvSec(_t0).toFixed(1)} 秒）`;
    $('#movementImportResult').textContent=msg;uiToast(msg,'ok');
    window.mvImportData=null;window.movementImportPreviewRows=[];window.movementImportToken=null;window.pvDeleted_movements=[];
    await loadItems();await loadMoves();
    _done=true;
  }catch(e){$('#movementImportResult').textContent=e.message}
  finally{pvEnd(_btn,_done)}
}

$('#movementImportForm').onsubmit=async e=>{
  e.preventDefault();
  const file=e.target.elements.file.files[0];if(!file)return;
  $('#movementImportMsg').textContent='正在读取并校验入库明细…';$('#movementImportPreview').innerHTML='';
  try{
    const r=await fetch('/api/import-movements-preview',{method:'POST',headers:{
      'Content-Type':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      'X-CSRF-Token':csrf},body:file});
    const d=await r.json();if(!r.ok)throw Error(d.error||'无法读取表格');
    renderMovementImportPreview(d);
  }catch(err){$('#movementImportMsg').textContent=err.message}
};



/* ===== GB300 导入：可改表头行 / 字段映射 / 分页可编辑预览 ===== */
try{Object.assign(importFieldNames,{room:'机房/房间',rack:'机柜位置',sn:'SN',
  server_sn:'服务器 SN',node_sn:'NODE_SN',node:'NODE / U 位',node_u:'NODE / U 位',
  kp_name:'KP_NAME',bmc_mac:'BMC_MAC'});}catch(e){}

const GB_IMPORT_CONFIG={
  servers:{
    boxId:'serverImportPreview',msgId:'serverImportMsg',formId:'serverImportForm',
    headerId:'gbSrvHeaderRow',mappingId:'gbSrvMapping',bodyId:'gbSrvBody',
    pageInfoId:'gbSrvPageInfo',prevId:'gbSrvPrev',nextId:'gbSrvNext',
    confirmId:'confirmServerImport',resultId:'serverImportResult',
    dataKey:'gbServerRows',pageKey:'gbServerPage',stateKey:'gbServerData',
    mapFields:['room','rack','sn'],tableFields:['room','rack','sn'],numberFields:[],
    headers:['机房/房间','机柜位置','SN'],labels:serverFieldLabels,
    remapUrl:'/api/import-servers-remap',confirmUrl:'/api/import-servers',
    hint:'必需字段：机房/房间、机柜位置、SN。',
    confirmLabel:n=>'确认导入 '+n+' 条服务器记录？重复 SN 会阻止整批写入。',
    success:d=>'已导入 '+d.imported+' 条服务器记录。',
    after:async()=>{editingServerId=null;await loadServers()}
  },
  components:{
    boxId:'serverComponentImportPreview',msgId:'serverComponentImportMsg',formId:'serverComponentImportForm',
    headerId:'gbCmpHeaderRow',mappingId:'gbCmpMapping',bodyId:'gbCmpBody',
    pageInfoId:'gbCmpPageInfo',prevId:'gbCmpPrev',nextId:'gbCmpNext',
    confirmId:'confirmServerComponentImport',resultId:'serverComponentImportResult',
    dataKey:'gbComponentRows',pageKey:'gbComponentPage',stateKey:'gbComponentData',
    mapFields:['server_sn','node_sn','node','kp_name','bmc_mac'],
    tableFields:['server_sn','node_u','node_sn','kp_name','bmc_mac'],numberFields:['node_u'],
    headers:['服务器 SN','NODE / U 位','NODE_SN','KP_NAME','BMC_MAC'],labels:serverComponentFieldLabels,
    remapUrl:'/api/import-server-components-remap',confirmUrl:'/api/import-server-components',
    hint:'必需字段：SN、NODE_SN、NODE / U 位、KP_NAME；BMC_MAC 可选。',
    confirmLabel:n=>'确认按 SN 替换本文件涉及服务器的部件清单？共 '+n+' 条部件，未列出的 U 位显示为空位。',
    success:d=>'已导入 '+d.imported+' 条部件记录，更新 '+d.servers+' 台服务器。',
    after:async()=>{}
  }
};

function renderGbPreview(kind,d){
  const cfg=GB_IMPORT_CONFIG[kind];
  window[cfg.stateKey]=d;
  window[cfg.dataKey]=d.preview_rows||d.samples||[];
  window[cfg.pageKey]=0;window['pvDeleted_'+kind]=[];
  const idx=d.mapping_index||{},reverse={};
  Object.entries(idx).forEach(([f,i])=>{reverse[i]=f});
  const headerOpts=(d.header_candidates||[]).map(x=>`<option value="${x.row}"${x.row===d.header_row?' selected':''}>第 ${x.row} 行：${esc((Array.isArray(x.cells)?x.cells:[]).filter(v=>String(v).trim()).slice(0,6).join(' | ')||'（空行）')}</option>`).join('');
  const colIdxs=d.active_columns||(d.columns||[]).map((_,i)=>i);
  const colHtml=colIdxs.map(i=>{
    const name=(d.columns||[])[i]||'';
    const opts=['<option value="">— 忽略此列 —</option>'].concat(
      cfg.mapFields.map(f=>`<option value="${f}"${reverse[i]===f?' selected':''}>${esc(cfg.labels[f]||f)}</option>`)).join('');
    return `<div class="map-col"><label>第 ${i+1} 列 · ${esc(String(name||'（无表头）').slice(0,40))}</label><select data-col="${i}">${opts}</select></div>`;
  }).join('');
  const errors=(d.errors||[]).map(x=>`<li>${esc(x)}</li>`).join('');
  const warnings=(d.warnings||[]).map(x=>`<li>${esc(x)}</li>`).join('');
  const mapping=Object.entries(d.mapping||{}).map(([k,v])=>`<span class="badge">${esc(cfg.labels[k]||k)} ← ${esc(v)}</span>`).join(' ');
  $('#'+cfg.msgId).textContent=`已选第 ${d.header_row} 行为表头，共 ${d.rows} 条记录。${d.can_import?'校验通过，可导入。':'请调整下面的表头行或字段对应后重新校验。'}`;
  $('#'+cfg.boxId).innerHTML=`<div class="card">
    <div class="import-section"><b>① 表头所在行</b>
      <div class="map-row"><select id="${cfg.headerId}">${headerOpts||`<option value="${d.header_row}">第 ${d.header_row} 行</option>`}</select><button type="button" class="secondary" onclick="applyGbMapping('${kind}')">应用修改并重新校验</button></div>
      <p class="hint">自动识别会扫描前 30 个非空行；表头不在第一行或识别错了，直接在这里选。</p></div>
    <div class="import-section"><b>② 字段对应（每列选一个字段，已隐藏 ${d.hidden_columns||0} 个空列）</b>
      <div class="import-mapping">${mapping||'<span class="hint">暂未识别到字段，请手动指定</span>'}</div>
      <details class="mapping-details"${d.can_import&&Object.keys(d.mapping||{}).length?'':' open'}><summary>点击调整字段对应（逐列指定 / 重新校验）</summary><div id="${cfg.mappingId}" class="import-mapping-grid">${colHtml}</div></details>
      <p class="hint">${cfg.hint}</p></div>
    ${warnings?`<p><b>提示</b><ul>${warnings}</ul></p>`:''}
    ${errors?`<p class="import-errors"><b>错误（整批不会导入）</b><ul>${errors}</ul></p>`:''}
    <div class="import-section"><b>③ 预览数据（可直接修改）</b>
      <div class="import-pager"><button type="button" class="secondary" id="${cfg.prevId}" onclick="switchGbPage('${kind}',-1)">上一页</button><span id="${cfg.pageInfoId}"></span><button type="button" class="secondary" id="${cfg.nextId}" onclick="switchGbPage('${kind}',1)">下一页</button></div>
      <div class="import-pager"><button type="button" class="secondary" id="${kind}DeletedRestore" onclick="pvRestore('gb','${kind}')">恢复已删除的行</button><span id="${kind}DeletedInfo" class="hint"></span></div>
      <p class="hint">不需要的行可以逐行删除；确认时只会写入当前预览中的行。</p>
      <div class="tablewrap preview-table"><table><thead><tr>${cfg.headers.map(h=>`<th>${esc(h)}</th>`).join('')}<th>操作</th></tr></thead><tbody id="${cfg.bodyId}"></tbody></table></div></div>
    ${(d.token||d.edit_token)?`<div class="import-section import-confirm-bar"><div class="confirm-bar-meta" id="${kind}ConfirmMeta"></div><button type="button" id="${cfg.confirmId}">确认导入 ${d.rows} 条</button><div id="${cfg.resultId}" class="status" aria-live="polite"></div></div>`:''}
  </div>`;
  renderGbPage(kind);pvUpdateBar('gb',kind);
  if(d.token||d.edit_token)$('#'+cfg.confirmId).onclick=()=>confirmGbImport(kind,d.token||d.edit_token);
}

function renderGbPage(kind){
  const cfg=GB_IMPORT_CONFIG[kind];
  const rows=window[cfg.dataKey]||[];
  const start=(window[cfg.pageKey]||0)*IMPORT_PAGE_SIZE;
  const html=rows.slice(start,start+IMPORT_PAGE_SIZE).map((x,i)=>`<tr>${cfg.tableFields.map(f=>`<td>${previewCell(x[f],i,f,cfg.numberFields.includes(f)?'number':'text')}</td>`).join('')}<td><button type="button" class="secondary" onclick="pvDelete('gb','${kind}',${i})">删除</button></td></tr>`).join('');
  const body=$('#'+cfg.bodyId);
  if(body)body.innerHTML=html||`<tr><td colspan="${cfg.tableFields.length+1}" class="hint">没有可预览的数据行；可点「恢复已删除的行」</td></tr>`;
  const total=Math.max(1,Math.ceil(rows.length/IMPORT_PAGE_SIZE));
  const info=$('#'+cfg.pageInfoId);
  if(info)info.textContent=`第 ${rows.length?start+1:0}–${Math.min(start+IMPORT_PAGE_SIZE,rows.length)} 条 / 共 ${rows.length} 条 · 第 ${(window[cfg.pageKey]||0)+1}/${total} 页`;
  const prev=$('#'+cfg.prevId),next=$('#'+cfg.nextId);
  if(prev)prev.disabled=(window[cfg.pageKey]||0)<=0;
  if(next)next.disabled=(window[cfg.pageKey]||0)>=total-1;
}

function collectGbVisible(kind){
  const cfg=GB_IMPORT_CONFIG[kind],box=$('#'+cfg.boxId);
  if(!box)return;
  const rows=window[cfg.dataKey]||[],start=(window[cfg.pageKey]||0)*IMPORT_PAGE_SIZE;
  rows.slice(start,start+IMPORT_PAGE_SIZE).forEach((row,i)=>{
    cfg.tableFields.forEach(f=>{
      const el=box.querySelector(`[data-row="${i}"][data-field="${f}"]`);
      if(!el||el.readOnly)return;
      row[f]=cfg.numberFields.includes(f)?(el.value.trim()===''?'':Number(el.value)):el.value.trim();
    });
  });
}

function switchGbPage(kind,delta){
  collectGbVisible(kind);
  const cfg=GB_IMPORT_CONFIG[kind];
  const total=Math.max(1,Math.ceil((window[cfg.dataKey]||[]).length/IMPORT_PAGE_SIZE));
  window[cfg.pageKey]=Math.min(total-1,Math.max(0,(window[cfg.pageKey]||0)+delta));
  renderGbPage(kind);
}

async function applyGbMapping(kind){
  const cfg=GB_IMPORT_CONFIG[kind],d=window[cfg.stateKey];
  if(!d||!d.edit_token){$('#'+cfg.msgId).textContent='预览已失效，请重新上传 Excel 文件';return}
  const mapping_index={},used={};let conflict=null;
  document.querySelectorAll('#'+cfg.mappingId+' [data-col]').forEach(sel=>{
    const f=sel.value;if(!f)return;
    if(used[f]!==undefined&&!conflict)conflict=cfg.labels[f]||f;
    used[f]=Number(sel.dataset.col);mapping_index[f]=Number(sel.dataset.col);
  });
  if(conflict){uiToast('字段「'+conflict+'」被指定了多列，请给每个字段只留一列。');return}
  if(!await uiConfirm('重新校验会按原始 Excel 重新抽取数据，之前在预览表格里手改的内容需要重填。继续吗？'))return;
  const header_row=Number($('#'+cfg.headerId).value)||d.header_row;
  $('#'+cfg.msgId).textContent='正在按新的表头行和字段对应重新校验…';
  try{
    const r=await api(cfg.remapUrl,{method:'POST',body:JSON.stringify({edit_token:d.edit_token,header_row,mapping_index})});
    renderGbPreview(kind,r);
  }catch(e){$('#'+cfg.msgId).textContent=e.message}
}

async function confirmGbImport(kind,token){
  let _done=false;
  const _t0=typeof performance!=='undefined'&&performance.now?performance.now():Date.now(),_btn=pvBtn('#'+GB_IMPORT_CONFIG[kind].confirmId);
  collectGbVisible(kind);pvBegin(_btn,'校验中…');
  const cfg=GB_IMPORT_CONFIG[kind],rows=window[cfg.dataKey]||[];
  if(!rows.length){const m='预览中没有可导入的数据；可点「恢复已删除的行」或重新上传';uiToast(m);$('#'+cfg.resultId).textContent=m;pvEnd(_btn);return}
  if(kind==='components'){
    const by={};rows.forEach(r=>{const sn=r.server_sn||'';by[sn]=(by[sn]||0)+1});
    const short=Object.entries(by).filter(x=>x[1]<37);
    if(short.length){
      const detail=short.map(x=>`${x[0]}：${x[1]} 个（少 ${37-x[1]}）`).join('\n');
      if(!await uiConfirm(`以下服务器部件少于 37 个 U 位，确认后会按当前行数替换原清单：\n${detail}\n\n仍要继续吗？`,'部件数量不足')){pvEnd(_btn);return}
    }
  }
  if(!await uiConfirm(cfg.confirmLabel(rows.length))){pvEnd(_btn);return}
  try{
    pvBegin(_btn,'写入中…');
    const r=await api(cfg.confirmUrl,{method:'POST',body:JSON.stringify({token,rows})});
    const msg=cfg.success(r)+`（${rows.length} 条，耗时 ${pvSec(_t0).toFixed(1)} 秒）`;
    $('#'+cfg.resultId).textContent=msg;uiToast(msg,'ok');
    window['pvDeleted_'+kind]=[];
    await cfg.after();
    _done=true;
  }catch(e){$('#'+cfg.resultId).textContent=e.message}
  finally{pvEnd(_btn,_done)}
}



/* ===== 服务器备件：已归档孤儿清理 ===== */
async function deleteServerSpare(id){
  if(!await uiConfirm('确认彻底删除这条已归档的备件及其状态历史？更换操作记录会保留，此操作不可撤销。'))return;
  try{
    await api('/api/server-spares/'+id,{method:'DELETE'});
    uiToast('已清理归档备件','ok');
    await loadSpares();
  }catch(e){uiToast(e.message)}
}



/* ===== Excel 预览：删除 / 恢复行（四条导入共用） ===== */
function pvStateKey(which,kind){return 'pvDeleted_'+(which==='gb'?kind:which)}
function pvCfg(which,kind){
  if(which==='items')return {
    data:'itemImportPreviewRows',page:'importPage',size:IMPORT_PAGE_SIZE,
    collect:()=>collectVisiblePreview(),render:()=>renderImportPage(),
    info:'#itemDeletedInfo',restore:'#itemDeletedRestore',btn:'#confirmImport',meta:'#itemConfirmMeta',
    label:n=>'确认导入 '+n+' 条'};
  if(which==='movements')return {
    data:'movementImportPreviewRows',page:'mvImportPage',size:MOVEMENT_PAGE_SIZE,
    collect:()=>collectMovementVisible(),render:()=>renderMovementPage(),
    info:'#mvDeletedInfo',restore:'#mvDeletedRestore',btn:'#movementConfirmBtn',meta:'#mvConfirmMeta',
    label:n=>'确认导入 '+n+' 条入库明细'};
  const g=GB_IMPORT_CONFIG[kind];
  return {data:g.dataKey,page:g.pageKey,size:IMPORT_PAGE_SIZE,
    collect:()=>collectGbVisible(kind),render:()=>renderGbPage(kind),
    info:'#'+kind+'DeletedInfo',restore:'#'+kind+'DeletedRestore',btn:'#'+g.confirmId,meta:'#'+kind+'ConfirmMeta',
    label:n=>'确认导入 '+n+' 条'};
}
function pvDelete(which,kind,i){
  const c=pvCfg(which,kind);c.collect();
  const rows=window[c.data]||[],start=(window[c.page]||0)*c.size,at=start+Number(i);
  if(!(at>=0&&at<rows.length))return;
  rows.forEach((r,i)=>{if(typeof r._pvSeq!=='number')r._pvSeq=i});
  const key=pvStateKey(which,kind);
  (window[key]||(window[key]=[])).push({pos:at,row:rows[at]});
  rows.splice(at,1);
  const total=Math.max(1,Math.ceil(rows.length/c.size));
  if((window[c.page]||0)>total-1)window[c.page]=total-1;
  c.render();pvUpdateBar(which,kind);
}
function pvRestore(which,kind){
  const c=pvCfg(which,kind);c.collect();
  const key=pvStateKey(which,kind),list=(window[key]||[]).slice();
  const rows=window[c.data]||(window[c.data]=[]);
  const all=rows.concat(list.map(x=>x.row));
  all.forEach((r,i)=>{if(typeof r._pvSeq!=='number')r._pvSeq=i});
  all.sort((a,b)=>a._pvSeq-b._pvSeq);
  window[c.data]=all;
  window[key]=[];c.render();pvUpdateBar(which,kind);
}
function pvUpdateBar(which,kind){
  const c=pvCfg(which,kind),rows=window[c.data]||[];
  const n=(window[pvStateKey(which,kind)]||[]).length;
  const info=$(c.info);if(info)info.textContent=n?`已删除 ${n} 行（确认导入时不会写入）`:'';
  const rst=$(c.restore);if(rst)rst.disabled=n===0;
  const btn=$(c.btn);if(btn){btn.textContent=c.label(rows.length);btn.disabled=rows.length===0}
  if(which==='movements'){
    const el=$('#mvQtyInfo');
    if(el){const miss=rows.filter(r=>!(Number(r.qty)>0)).length;
      el.textContent=rows.length?(miss?`还有 ${miss} 行未填写入库数量（需大于 0），请在下方补填或删除不需要的行。`:'数量已填写完整，可以确认导入。'):'';}
  }
  const meta=$(c.meta);
  if(meta){
    const gbCfg=(typeof GB_IMPORT_CONFIG!=='undefined'&&GB_IMPORT_CONFIG[kind]);
    const d=which==='items'?(window.currentImportData||{}):which==='movements'?(window.mvImportData||{}):(gbCfg?(window[gbCfg.stateKey]||{}):{});
    const errs=(d.errors||[]).length, ok=d.can_import!==false&&errs===0;
    const parts=[`当前 ${rows.length} 行`];
    if(n)parts.push(`已删 ${n} 行`);
    parts.push(rows.length===0?'无可导入数据':(ok?'校验通过':(errs?`${errs} 处错误`:'待调整')));
    meta.textContent=parts.join(' · ');
    meta.className='confirm-bar-meta '+(rows.length===0?'is-idle':ok?'is-ok':'is-err');
  }
}



/* ===== 操作反馈：阶段文案与耗时 ===== */
function pvBtn(sel){try{return $(sel)}catch(e){return null}}
function pvBegin(btn,text){if(btn){if(btn.dataset.pvPrev===undefined)btn.dataset.pvPrev=btn.textContent;btn.textContent=text;btn.disabled=true}return btn}
function pvEnd(btn,done){if(!btn)return;if(done){btn.textContent='已完成';btn.disabled=true;delete btn.dataset.pvPrev;return}if(btn.dataset.pvPrev!==undefined){btn.textContent=btn.dataset.pvPrev;btn.disabled=false;delete btn.dataset.pvPrev}}
function pvSec(t0){return ((typeof performance!=='undefined'&&performance.now?performance.now():Date.now())-t0)/1000}
