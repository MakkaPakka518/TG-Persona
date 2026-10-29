'use strict';
// 原前端代码提取后运行；不访问服务器，不依赖第三方 DOM 库。
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(path.join(__dirname,'../web/index.html'),'utf8');
const js=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const elements=new Map(),calls=[],intervals=new Map(),readers=[];let intervalId=0,nodeId=0;
class MockFileReader{
  constructor(){readers.push(this)}
  readAsDataURL(file){this.file=file}
  abort(){this.aborted=true;if(this.onabort)this.onabort()}
  complete(result){this.result=result;this.onload()}
  fail(){this.onerror()}
}
const sampleImage='data:image/png;base64,YWJjZA==';
function configureAI(ai=true,ocr=true,key='dummy-test-key'){
  element('#sAiBase').value=' https://dummy.invalid/v1 ';
  element('#sAiKey').value=key;element('#sAiModel').value=' dummy-model ';
  element('#sAiOn').checked=ai;element('#sOcrOn').checked=ocr;
}
function selectImage(file={type:'image/png',size:128},image=sampleImage){
  element('#captchaTestFile').files=file?[file]:[];run('previewCaptchaFile()');
  if(file&&image)readers.at(-1).complete(image);
}
function element(id){
  if(!elements.has(id)){
    const el={innerHTML:'',textContent:'',children:[],style:{},disabled:false,src:'',listeners:{},classList:{add(){},remove(){},toggle(){}},scrollIntoView(){},addEventListener(t,fn){this.listeners[t]=fn},contains(){return true},removeAttribute(k){delete this[k]},appendChild(node){this.children.push(node);return node},replaceChildren(...nodes){this.children=nodes;this.textContent=''}};
    let value='';Object.defineProperty(el,'value',{get(){return value},set(v){value=String(v)}});
    elements.set(id,el);
  }
  return elements.get(id);
}
const context=vm.createContext({console,Buffer,Date,JSON,String,Error,Promise,FileReader:MockFileReader,
  fetch(){throw new Error('测试禁止真实网络请求')},
  window:{confirm(message){calls.push(['confirm',message]);return false}},
  document:{querySelector:element,getElementById:id=>element('#'+id),body:{style:{}},createElement(){
    const node=element('#mock-node-'+(++nodeId));
    Object.defineProperty(node,'innerHTML',{get(){return ''},set(){throw new Error('备份名称和 metadata 必须用 textContent')}});
    return node;
  }},
  toast:(...args)=>calls.push(['toast',...args]),
  api:async(...args)=>{calls.push(['api',...args]);return {}},
  addGroup:(...args)=>calls.push(['addGroup',...args]),removeGroup:id=>calls.push(['removeGroup',id]),
  loadSessions(){},loadAutos(){},bootstrap(){},
  btoa:s=>Buffer.from(s,'binary').toString('base64'),unescape,encodeURIComponent,
  setTimeout(){},
  setInterval(fn){const id=++intervalId;intervals.set(id,fn);return id},clearInterval(id){intervals.delete(id)}
});
function run(s){return vm.runInContext(s,context)}
function slice(start,end){const a=js.indexOf(start),b=js.indexOf(end,a+start.length);assert(a>=0&&b>a,start);return js.slice(a,b)}
run(js.split('\n').find(line=>line.startsWith('const $=')));
run(js.split('\n').find(line=>line.startsWith('const esc=')));
run(slice('let allGroups=[];','async function loadDialogs'));
run(slice('function autoDesc(a){','function dupAuto'));
run('let autos=[],editAuto=null;');
run(slice('function editAutoItem(id){','function pickGrabAct(v){'));
run(slice('function openAutoForm(type){','async function toggleAuto(id){'));
function prepareSignin(overrides={}){
  run("cancelAutoEdit('signin')");
  element('#sName').value='假签到任务';element('#sTarget').value='@dummy_bot';element('#sCmd').value='/sign';
  for(const [id,value] of Object.entries(overrides))element(id).value=value;
}
function editSignin(delays={}){
  context.signinInput={id:42,type:'signin',name:'假签到任务',target:'@dummy_bot',command:'/sign',win_start:'08:00',win_end:'12:00',enabled:0,...delays};
  run('autos=[signinInput];editAutoItem(42)');
}
function mockBackups(count=3,extra={}){
  return {items:Array.from({length:count},(_,i)=>({name:'backup-mock-'+i,bytes:1024,created_at:'2026-01-01T00:00:00Z',kind:'update'})),count,total_bytes:count*1024,cleanup_allowed:true,reason:'',...extra};
}
async function loadMockBackups(data=mockBackups()){
  context.api=async(p,o)=>{assert.strictEqual(p,'/api/backups');assert.strictEqual(o,undefined);return data};
  await run('loadBackups()');
}
run(slice('async function reloadCaptcha(){','async function panelLogin'));
run(slice('async function changePw(){','async function logoutAll'));
run(slice('let updTimer=null','async function checkUpdate'));
run(slice('// ---------- AI 与验证码测试 ----------','async function changePw(){'));
for(const id of ['#aiTestResult','#captchaTestResult','#backupsInfo','#backupsReason','#backupsList','#backupsResult']){
  Object.defineProperty(element(id),'innerHTML',{get(){return ''},set(){throw new Error('测试结果必须用 textContent')}});
}
let count=0;
async function test(name,fn){await fn();count++;console.log('通过：'+name)}
(async()=>{
  await test('恶意群名只作为文本传递，按钮不含内联脚本',async()=>{
    const name="');globalThis.__injected=1;//<img src=x onerror=globalThis.__injected=2>";
    context.groupInput=[{chat_id:'-100123',name,watched:false}];
    run('allGroups=groupInput;globalThis.__injected=0;renderGroups()');
    const markup=element('#gAll').innerHTML;
    assert(!/onclick=/.test(markup));assert(!markup.includes('<img'));
    assert(markup.includes('&lt;img'));assert(markup.includes('data-add-group="-100123"'));
    const btn={dataset:{addGroup:'-100123'}};
    element('#gAll').listeners.click({target:{closest:()=>btn}});
    assert.deepStrictEqual(calls.filter(x=>x[0]==='addGroup').at(-1),['addGroup','-100123',name]);
    assert.strictEqual(run('globalThis.__injected'),0);
  });
  await test('数字群ID和空群名正常渲染',async()=>{
    context.groupInput=[{chat_id:-10012,name:null,watched:false}];run('allGroups=groupInput;renderGroups()');
    assert(element('#gAll').innerHTML.includes('data-add-group="-10012"'));
    element('#gSearch').value='hello';run('renderGroups()');
    assert(element('#gAll').innerHTML.includes('没有更多群'));element('#gSearch').value='';
  });
  await test('取消监听通过事件委托传递ID',async()=>{
    element('#gWatched').listeners.click({target:{closest:()=>({dataset:{removeGroup:'-10012'}})}});
    assert.deepStrictEqual(calls.at(-1),['removeGroup','-10012']);
  });
  await test('位图验证码直接显示且不泄露答案',async()=>{
    context.api=async()=>({image:'data:image/png;base64,aGVsbG8=',required:true});
    await run('reloadCaptcha()');assert.strictEqual(element('#lgCapImg').src,'data:image/png;base64,aGVsbG8=');
  });
  await test('兼容旧SVG验证码便于跨版本升级',async()=>{
    context.api=async()=>({svg:'<svg></svg>'});await run('reloadCaptcha()');
    assert(element('#lgCapImg').src.startsWith('data:image/svg+xml;base64,'));
  });
  await test('拒绝将远程或脚本URL作为验证码图片',async()=>{
    context.api=async()=>({image:'javascript:alert(1)'});await run('reloadCaptcha()');
    assert.strictEqual(element('#lgCapImg').src,undefined);
  });
  await test('旧日期不显示今日已签',async()=>{
    const a={type:'signin',target:'@test',command:'/sign',win_start:'08:00',win_end:'12:00',last_at:'2000-01-01',signed_today:false};
    context.autoInput=a;assert(!run('autoDesc(autoInput)').includes('今日已签'));assert(run('autoDesc(autoInput)').includes('上次成功'));
    a.signed_today=true;assert(run('autoDesc(autoInput)').includes('今日已签'));
  });
  await test('签到表单包含两段中文秒数标签、默认值及范围步进',async()=>{
    const form=html.slice(html.indexOf('id="af-signin"'),html.indexOf('id="af-grabcodes"'));
    for(const [id,label,value] of [['sCommandDelay','发送命令后延迟（秒）','1.6'],['sButtonDelay','点击按钮后延迟（秒）','2']]){
      assert(form.includes('<label for="'+id+'">'+label+'</label>'));
      const input=form.match(new RegExp('<input\\b[^>]*id="'+id+'"[^>]*>'))[0];
      for(const attr of ['type="number"','value="'+value+'"','min="0"','max="120"','step="0.1"','required'])assert(input.includes(attr));
    }
    assert(form.includes('发送命令 → 等待命令后延迟 → 点击按钮 → 等待按钮后延迟 → 后续验证码步骤'));
    assert(form.includes('0 秒表示立即进行'));
  });
  await test('新增及取消编辑恢复 1.6 秒和 2 秒默认值',async()=>{
    editSignin({command_delay:0,button_delay:0});run("closeAutoForm('signin');openAutoForm('signin')");
    assert.strictEqual(element('#sCommandDelay').value,'1.6');assert.strictEqual(element('#sButtonDelay').value,'2');
    assert.strictEqual(run('editAuto'),null);
  });
  await test('签到编辑回填保留数值零、字符串零及独立小数',async()=>{
    for(const [command_delay,button_delay] of [[0,0],['0','0'],[0,2.7],[3.1,0],[120,0.1]]){
      editSignin({command_delay,button_delay});
      assert.strictEqual(element('#sCommandDelay').value,String(command_delay));
      assert.strictEqual(element('#sButtonDelay').value,String(button_delay));
    }
  });
  await test('旧任务缺失或 null 延迟时编辑回填默认且不覆盖另一段零',async()=>{
    for(const delays of [{},{command_delay:null,button_delay:null},{command_delay:0},{button_delay:0}]){
      editSignin(delays);
      assert.strictEqual(element('#sCommandDelay').value,String(delays.command_delay??1.6));
      assert.strictEqual(element('#sButtonDelay').value,String(delays.button_delay??2));
    }
  });
  await test('新增保存 JSON 包含数值默认延迟，空值同样回退默认',async()=>{
    for(const overrides of [{},{'#sCommandDelay':'','#sButtonDelay':' '}]){
      prepareSignin(overrides);let request;
      context.api=async(p,o)=>{request=[p,o];return {}};await run("saveAuto('signin')");
      assert.strictEqual(request[0],'/api/autos');assert.strictEqual(request[1].method,'POST');
      const body=JSON.parse(request[1].body);
      assert.strictEqual(body.command_delay,1.6);assert.strictEqual(body.button_delay,2);
      assert.strictEqual(body.type,'signin');assert.strictEqual(body.target,'@dummy_bot');
    }
  });
  await test('新增保存零延迟 JSON 为数值零而非默认值',async()=>{
    prepareSignin({'#sCommandDelay':'0','#sButtonDelay':'0'});let body;
    context.api=async(p,o)=>{body=JSON.parse(o.body);return {}};await run("saveAuto('signin')");
    assert.strictEqual(body.command_delay,0);assert.strictEqual(body.button_delay,0);
  });
  await test('编辑 PUT 保存独立延迟和边界值且保持启用状态',async()=>{
    for(const [command_delay,button_delay] of [[0,120],[120,0],[0.1,3.7]]){
      editSignin();element('#sCommandDelay').value=command_delay;element('#sButtonDelay').value=button_delay;
      let request;context.api=async(p,o)=>{request=[p,o];return {}};await run("saveAuto('signin')");
      assert.strictEqual(request[0],'/api/autos/42');assert.strictEqual(request[1].method,'PUT');
      const body=JSON.parse(request[1].body);
      assert.strictEqual(body.command_delay,command_delay);assert.strictEqual(body.button_delay,button_delay);
      assert.strictEqual(body.enabled,0);
    }
  });
  await test('任一签到延迟越界、非数值或步进不符不会调用接口',async()=>{
    let sent=0;context.api=async()=>{sent++;return {}};
    for(const id of ['#sCommandDelay','#sButtonDelay'])for(const value of ['-0.1','120.1','NaN','Infinity','1.05']){
      prepareSignin({[id]:value});await run("saveAuto('signin')");
      assert(calls.at(-1)[1].includes('0–120 秒'));assert(calls.at(-1)[1].includes('0.1 秒'));
    }
    assert.strictEqual(sent,0);
  });
  await test('签到摘要显示默认、零及独立延迟，异常文本安全转义',async()=>{
    for(const delays of [{},{command_delay:0,button_delay:0},{command_delay:0.1,button_delay:120}]){
      context.autoInput={type:'signin',target:'@dummy_bot',command:'/sign',win_start:'08:00',win_end:'12:00',...delays};
      const desc=run('autoDesc(autoInput)');
      assert(desc.includes('命令后 '+(delays.command_delay??1.6)+'秒'));
      assert(desc.includes('按钮后 '+(delays.button_delay??2)+'秒'));
    }
    context.autoInput.command_delay='<img src=x>';assert(!run('autoDesc(autoInput)').includes('<img'));
    assert(run('autoDesc(autoInput)').includes('&lt;img'));
  });
  await test('连点下拉框提供刷新默认及复用两种明确模式',async()=>{
    const select=html.match(/<select id="sRefreshButtons">([\s\S]*?)<\/select>/)[1];
    assert(select.includes('<option value="1" selected>刷新按钮状态'));
    assert(select.includes('<option value="0">复用同一按钮'));
    assert(select.includes('适合会变化按钮')&&select.includes('不重新获取消息'));
  });
  await test('连点模式编辑保留数值零及字符串零，旧任务默认一',async()=>{
    for(const data of [{refresh_buttons:0},{refresh_buttons:'0'},{refresh_buttons:1},{refresh_buttons:'1'},{},{refresh_buttons:null}]){
      editSignin(data);assert.strictEqual(element('#sRefreshButtons').value,String(data.refresh_buttons??1));
    }
  });
  await test('取消编辑和关闭后新建都恢复默认刷新模式',async()=>{
    editSignin({refresh_buttons:0});run("cancelAutoEdit('signin')");
    assert.strictEqual(element('#sRefreshButtons').value,'1');assert.strictEqual(run('editAuto'),null);
    editSignin({refresh_buttons:0});run("closeAutoForm('signin');openAutoForm('signin')");
    assert.strictEqual(element('#sRefreshButtons').value,'1');
  });
  await test('新增连点模式保存数值零或一，默认及空选项为一',async()=>{
    for(const [value,expected] of [[undefined,1],['0',0],['1',1],['',1]]){
      prepareSignin(value===undefined?{}:{'#sRefreshButtons':value});let request;
      context.api=async(p,o)=>{request=[p,o];return {}};await run("saveAuto('signin')");
      assert.strictEqual(request[0],'/api/autos');assert.strictEqual(request[1].method,'POST');
      const body=JSON.parse(request[1].body);assert.strictEqual(body.refresh_buttons,expected);
      assert.strictEqual(typeof body.refresh_buttons,'number');
      assert.strictEqual(body.command_delay,1.6);assert.strictEqual(body.button_delay,2);
    }
  });
  await test('编辑 PUT 保存零或一模式且不改变延迟和启用状态',async()=>{
    for(const refresh_buttons of [0,1]){
      editSignin({refresh_buttons,command_delay:0,button_delay:3.7});let request;
      context.api=async(p,o)=>{request=[p,o];return {}};await run("saveAuto('signin')");
      assert.strictEqual(request[0],'/api/autos/42');assert.strictEqual(request[1].method,'PUT');
      const body=JSON.parse(request[1].body);assert.strictEqual(body.refresh_buttons,refresh_buttons);
      assert.strictEqual(body.command_delay,0);assert.strictEqual(body.button_delay,3.7);assert.strictEqual(body.enabled,0);
    }
  });
  await test('签到摘要显示默认刷新或复用模式并不插入异常字段HTML',async()=>{
    for(const [value,label] of [[undefined,'刷新按钮状态'],[null,'刷新按钮状态'],[1,'刷新按钮状态'],[0,'复用同一按钮'],['0','复用同一按钮'],['<img src=x>','刷新按钮状态']]){
      context.autoInput={type:'signin',target:'@dummy',command:'/sign',click_btn:1,refresh_buttons:value};
      const desc=run('autoDesc(autoInput)');assert(desc.includes(label));assert(!desc.includes('<img'));
    }
    context.autoInput.click_btn=0;assert(!run('autoDesc(autoInput)').includes('刷新按钮状态'));
  });
  await test('备份管理在更新卡片内，默认优先保留最新且清理按钮初始禁用',async()=>{
    const card=html.slice(html.indexOf('id="updInfo"'),html.indexOf('id="sCooldown"'));
    for(const id of ['backupsInfo','backupsReason','backupsList','backupsResult','backupsLoadBtn','backupsKeepBtn','backupsAllBtn'])assert(card.includes('id="'+id+'"'));
    assert(card.indexOf('id="backupsKeepBtn"')<card.indexOf('id="backupsAllBtn"'));
    assert(/id="backupsKeepBtn"[^>]*disabled/.test(card));assert(/id="backupsAllBtn"[^>]*disabled/.test(card));
    assert(card.includes('推荐'));run('syncBackupButtons()');assert(element('#backupsKeepBtn').disabled&&element('#backupsAllBtn').disabled);
    let sent=0;context.api=async()=>{sent++;return {}};await run("cleanupBackups('all')");assert.strictEqual(sent,0);
  });
  await test('GET 备份展示份数空间及名称元数据，所有文本均拒绝 HTML sink',async()=>{
    const name='<img src=x onerror=globalThis.__backupInjected=1>',created_at='<script>bad()</script>',kind='<b>kind</b>';
    await loadMockBackups(mockBackups(1,{items:[{name,bytes:2048,created_at,kind}],total_bytes:2048}));
    assert.strictEqual(element('#backupsInfo').textContent,'备份共 1 份 · 占用 2.0 KiB');
    const rows=element('#backupsList').children;assert.strictEqual(rows.length,1);
    assert.strictEqual(rows[0].children[0].textContent,name);
    assert.strictEqual(rows[0].children[1].textContent,'大小：2.0 KiB · 创建时间：'+created_at+' · 类型：'+kind);
    assert.strictEqual(run('globalThis.__backupInjected'),undefined);
    assert(element('#backupsKeepBtn').disabled);assert(!element('#backupsAllBtn').disabled);
    await loadMockBackups(mockBackups());assert.strictEqual(element('#backupsList').children.length,3);
  });
  await test('空备份及无清理许可默认禁止删除，理由作为文本显示',async()=>{
    await loadMockBackups(mockBackups(0));assert.strictEqual(element('#backupsList').textContent,'暂无备份');
    assert(element('#backupsKeepBtn').disabled&&element('#backupsAllBtn').disabled);
    for(const cleanup_allowed of [false,undefined,'true']){
      const reason='活动任务中 <script>alert(1)</script>';
      await loadMockBackups(mockBackups(3,{cleanup_allowed,reason}));
      assert(element('#backupsReason').textContent.includes(reason));assert(element('#backupsKeepBtn').disabled&&element('#backupsAllBtn').disabled);
      let sent=0;context.api=async()=>{sent++;return {}};
      const confirms=calls.filter(x=>x[0]==='confirm').length;
      await run("cleanupBackups('keep_latest');cleanupBackups('all')");
      assert.strictEqual(sent,0);assert.strictEqual(calls.filter(x=>x[0]==='confirm').length,confirms);
    }
  });
  await test('两种备份删除操作取消确认都不发请求，提示不可回滚且只删备份',async()=>{
    await loadMockBackups();let sent=0;context.api=async()=>{sent++;return {}};
    context.window.confirm=message=>{calls.push(['confirm',message]);return false};
    for(const mode of ['keep_latest','all']){
      await run("cleanupBackups('"+mode+"')");const text=calls.at(-1)[1];
      for(const word of ['不可恢复','不可回滚','备份版本','只删除备份','不删除数据库','会话','venv'])assert(text.includes(word));
      assert(text.includes(mode==='keep_latest'?'保留最新一份':'删除全部备份'));
      assert(!element('#backupsKeepBtn').disabled&&!element('#backupsAllBtn').disabled);
    }
    assert.strictEqual(sent,0);
  });
  await test('推荐清理 POST keep_latest keep=1 并刷新列表及释放空间',async()=>{
    await loadMockBackups();context.window.confirm=()=>true;const requests=[];
    context.api=async(p,o)=>{requests.push([p,o]);return p==='/api/backups/cleanup'?{ok:true,deleted:['backup-mock-1','backup-mock-2'],freed_bytes:2048,remaining:1}:mockBackups(1)};
    await run("cleanupBackups('keep_latest')");assert.strictEqual(requests.length,2);
    assert.strictEqual(requests[0][0],'/api/backups/cleanup');assert.strictEqual(requests[0][1].method,'POST');
    assert.deepStrictEqual(JSON.parse(requests[0][1].body),{mode:'keep_latest',keep:1});
    assert.strictEqual(requests[1][0],'/api/backups');assert.strictEqual(requests[1][1],undefined);
    assert(element('#backupsResult').textContent.includes('已删除 2 份'));assert(element('#backupsResult').textContent.includes('释放 2.0 KiB'));
    assert(element('#backupsInfo').textContent.includes('共 1 份'));assert(element('#backupsKeepBtn').disabled&&!element('#backupsAllBtn').disabled);
  });
  await test('删除全部只发送 mode=all 并刷新为空列表',async()=>{
    await loadMockBackups();const requests=[];
    context.api=async(p,o)=>{requests.push([p,o]);return p==='/api/backups/cleanup'?{ok:true,deleted:['a','b','c'],freed_bytes:3072,remaining:0}:mockBackups(0)};
    await run("cleanupBackups('all')");assert.strictEqual(requests.length,2);
    assert.strictEqual(requests[0][1].method,'POST');assert.deepStrictEqual(JSON.parse(requests[0][1].body),{mode:'all'});
    assert.strictEqual(requests[1][0],'/api/backups');assert.strictEqual(element('#backupsList').textContent,'暂无备份');
    assert(element('#backupsResult').textContent.includes('剩余 0 份'));assert(element('#backupsKeepBtn').disabled&&element('#backupsAllBtn').disabled);
  });
  await test('备份列表请求中禁止重复加载和任何清理，完成后恢复按钮',async()=>{
    await loadMockBackups();let resolve,sent=0;
    context.api=()=>{sent++;return new Promise(r=>{resolve=r})};
    const pending=run('loadBackups()');assert(element('#backupsLoadBtn').disabled&&element('#backupsKeepBtn').disabled&&element('#backupsAllBtn').disabled);
    await run("loadBackups();cleanupBackups('all');cleanupBackups('keep_latest')");assert.strictEqual(sent,1);
    resolve(mockBackups());await pending;
    assert(!element('#backupsLoadBtn').disabled&&!element('#backupsKeepBtn').disabled&&!element('#backupsAllBtn').disabled);
  });
  await test('清理请求及后续列表刷新期间都防重复且保持按钮禁用',async()=>{
    await loadMockBackups();context.window.confirm=()=>true;let resolvePost,resolveGet;const requests=[];
    context.api=(p,o)=>{requests.push([p,o]);return new Promise(r=>{if(p==='/api/backups/cleanup')resolvePost=r;else resolveGet=r})};
    const pending=run("cleanupBackups('keep_latest')");
    assert(element('#backupsLoadBtn').disabled&&element('#backupsKeepBtn').disabled&&element('#backupsAllBtn').disabled);
    await run("cleanupBackups('all');cleanupBackups('keep_latest');loadBackups()");assert.strictEqual(requests.length,1);
    resolvePost({ok:true,deleted:['a','b'],freed_bytes:2048,remaining:1});await new Promise(resolve=>setImmediate(resolve));
    assert.strictEqual(requests.length,2);assert.strictEqual(requests[1][0],'/api/backups');
    assert(element('#backupsLoadBtn').disabled&&element('#backupsKeepBtn').disabled&&element('#backupsAllBtn').disabled);
    await run("cleanupBackups('all');loadBackups()");assert.strictEqual(requests.length,2);
    resolveGet(mockBackups(1));await pending;assert(!element('#backupsLoadBtn').disabled&&!element('#backupsAllBtn').disabled);
  });
  await test('系统更新活动状态额外拒绝清理，完成更新后恢复许可',async()=>{
    await loadMockBackups();context.updateInput={current:'mock-version',running:true};run('renderUpd(updateInput)');
    assert(element('#backupsKeepBtn').disabled&&element('#backupsAllBtn').disabled);
    let sent=0;context.api=async()=>{sent++;return {}};await run("cleanupBackups('all');cleanupBackups('keep_latest')");assert.strictEqual(sent,0);
    context.updateInput.running=false;run('renderUpd(updateInput)');assert(!element('#backupsKeepBtn').disabled&&!element('#backupsAllBtn').disabled);
  });
  await test('无效清理模式或仅剩一份时拒绝推荐清理且不弹确认',async()=>{
    await loadMockBackups(mockBackups(1));let sent=0,confirms=0;
    context.api=async()=>{sent++;return {}};context.window.confirm=()=>{confirms++;return true};
    await run("cleanupBackups('keep_latest');cleanupBackups('unexpected')");assert.strictEqual(sent,0);assert.strictEqual(confirms,0);
  });
  await test('列表错误与清理错误都走原有提示且失效旧清理许可',async()=>{
    await loadMockBackups();context.api=async()=>{throw new Error('mock list unavailable')};await run('loadBackups()');
    assert(element('#backupsInfo').textContent.includes('mock list unavailable'));assert.deepStrictEqual(calls.at(-1),['toast','mock list unavailable',1]);
    assert(!element('#backupsLoadBtn').disabled&&element('#backupsKeepBtn').disabled&&element('#backupsAllBtn').disabled);
    await loadMockBackups();context.window.confirm=()=>true;context.api=async()=>{throw new Error('mock cleanup unavailable')};
    await run("cleanupBackups('all')");assert(element('#backupsResult').textContent.includes('mock cleanup unavailable'));
    assert.deepStrictEqual(calls.at(-1),['toast','mock cleanup unavailable',1]);assert(!element('#backupsLoadBtn').disabled&&element('#backupsAllBtn').disabled);
    await loadMockBackups();assert(!element('#backupsKeepBtn').disabled);
  });
  await test('清理后列表刷新失败及 ok=false 不恢复清理许可或伪报成功',async()=>{
    await loadMockBackups();context.window.confirm=()=>true;
    context.api=async p=>{if(p==='/api/backups')throw new Error('mock refresh unavailable');return {ok:true,deleted:['a'],freed_bytes:1,remaining:2}};
    await run("cleanupBackups('all')");assert(element('#backupsResult').textContent.includes('mock refresh unavailable'));assert(element('#backupsAllBtn').disabled);
    await loadMockBackups();let sent=0;context.api=async()=>{sent++;return {ok:false,error:'mock denied'}};
    await run("cleanupBackups('all')");assert.strictEqual(sent,1);assert(element('#backupsResult').textContent.includes('mock denied'));assert(element('#backupsAllBtn').disabled);
  });
  await test('备份真实 api 包装通过 mock fetch 展示 409 活动冲突与 401 登录错误',async()=>{
    let gated=0;const requests=[];
    const apiContext=vm.createContext({fetch:async(p,o)=>{requests.push([p,o]);return {status:409,ok:false,json:async()=>({detail:'mock 活动任务中，禁止清理'})}},showLoginGate(){gated++}});
    vm.runInContext(slice('async function api(','const esc='),apiContext);
    await loadMockBackups();context.api=apiContext.api;context.window.confirm=()=>true;await run("cleanupBackups('keep_latest')");
    assert.strictEqual(requests.length,1);assert.strictEqual(requests[0][0],'/api/backups/cleanup');
    assert(element('#backupsResult').textContent.includes('mock 活动任务中'));assert(element('#backupsKeepBtn').disabled&&element('#backupsAllBtn').disabled);
    apiContext.fetch=async()=>({status:401});await run('loadBackups()');assert.strictEqual(gated,1);
    assert(element('#backupsInfo').textContent.includes('未登录面板'));assert(!element('#backupsLoadBtn').disabled&&element('#backupsAllBtn').disabled);
  });
  await test('改密码缺少原密码不会调用接口',async()=>{
    let sent=0;context.api=async()=>{sent++;return {}};
    element('#sNewPw').value='new-dummy-password';element('#sOldPw').value='';
    await run('changePw()');assert.strictEqual(sent,0);
    element('#sOldPw').value='old-dummy-password';await run('changePw()');assert.strictEqual(sent,1);
  });
  await test('更新完成后自动停止轮询',async()=>{
    context.api=async()=>({current:'1.0.4',running:true,phase:'restart'});
    await run('loadUpd(false)');assert.strictEqual(intervals.size,1);
    context.api=async()=>({current:'1.0.4',running:false,ok:true});await run('pollUpd()');
    assert.strictEqual(intervals.size,0);assert(element('#updInfo').innerHTML.includes('健康检查已通过'));
  });
  await test('重启中临时请求失败仍保持轮询',async()=>{
    context.api=async()=>({current:'1.0.4',running:true});await run('loadUpd(false)');
    context.api=async()=>{throw new Error('暂时重启')};await run('pollUpd()');assert.strictEqual(intervals.size,1);
  });
  await test('检查更新失败不会被吞掉且恢复按钮',async()=>{
    context.api=async()=>{throw new Error('mock network error')};
    await assert.rejects(()=>run('loadUpd(true)'),/mock network error/);
    assert.strictEqual(element('#updCheck').disabled,false);
  });
  await test('新增入口位于识别卡片且密钥为密码输入',async()=>{
    assert(/<input type="password" id="sAiKey"/.test(html));
    for(const id of ['aiTestBtn','aiTestResult','captchaTestFile','captchaTestPreview','captchaTestLength','captchaTestBtn','captchaTestResult']){
      assert(html.indexOf('id="'+id+'"')>html.indexOf('id="sAiModel"'));
      assert(html.indexOf('id="'+id+'"')<html.indexOf('id="aCntSign"'));
    }
  });
  await test('AI 测试使用未保存填写值、空 Key 且不受开关阻止',async()=>{
    configureAI(false,false,' ');let request;
    context.api=async(p,o)=>{request=[p,o];return {ok:true,reply:'测试成功',elapsed_ms:27}};
    await run('testAIConnection()');
    assert.strictEqual(request[0],'/api/ai/test');assert.strictEqual(request[1].method,'POST');
    assert.deepStrictEqual(JSON.parse(request[1].body),{ai_base:'https://dummy.invalid/v1',ai_key:'',ai_model:'dummy-model',ai_enabled:'0',ocr_enabled:'0'});
    assert(element('#aiTestResult').textContent.includes('连接测试成功'));
    assert(element('#aiTestResult').textContent.includes('实际回复：测试成功'));
    assert(element('#aiTestResult').textContent.includes('27 ms'));assert(!element('#aiTestBtn').disabled);
  });
  await test('AI 失败展示原因与非约定回复且只作为文本',async()=>{
    const reply='<img src=x onerror=alert(1)>';
    context.api=async()=>({ok:false,reply,error:'非约定回复 <script>',elapsed_ms:9});
    await run('testAIConnection()');const out=element('#aiTestResult').textContent;
    assert(out.includes(reply)&&out.includes('非约定回复 <script>')&&out.includes('连接测试失败'));
    assert(!element('#aiTestBtn').disabled);
  });
  await test('AI 返回未知或空结果不会冒充成功',async()=>{
    for(const data of [{ok:true,reply:'other'},{ok:false,reply:'测试成功'},{},{ok:true,reply:42}]){
      context.api=async()=>data;await run('testAIConnection()');
      assert(element('#aiTestResult').textContent.includes('连接测试失败'));
      assert(element('#aiTestResult').textContent.includes('原因：'));
      assert(!element('#aiTestBtn').disabled);
    }
  });
  await test('AI 忙时只发一次请求且异常后恢复按钮',async()=>{
    let reject,sent=0;context.api=()=>{sent++;return new Promise((_,r)=>{reject=r})};
    const pending=run('testAIConnection()');assert(element('#aiTestBtn').disabled);
    await run('testAIConnection()');assert.strictEqual(sent,1);reject(new Error('mock unavailable'));
    await pending;assert(!element('#aiTestBtn').disabled);
    assert(element('#aiTestResult').textContent.includes('mock unavailable'));
    assert.deepStrictEqual(calls.at(-1),['toast','mock unavailable',1]);
  });
  await test('选择图片仅本地预览、清理结果且无上传',async()=>{
    let sent=0;context.api=async()=>{sent++;return {}};
    element('#captchaTestResult').textContent='旧结果';selectImage();
    assert.strictEqual(sent,0);assert.strictEqual(element('#captchaTestPreview').src,sampleImage);
    assert.strictEqual(element('#captchaTestPreview').style.display,'block');
    assert.strictEqual(element('#captchaTestResult').textContent,'');assert(!element('#captchaTestBtn').disabled);
  });
  await test('拒绝超大、空文件和不支持的上传格式',async()=>{
    for(const file of [{type:'image/png',size:2*1024*1024+1},{type:'image/png',size:0},{type:'image/svg+xml',size:20},{type:'',size:20}]){
      const n=readers.length;element('#captchaTestFile').files=[file];run('previewCaptchaFile()');
      assert.strictEqual(readers.length,n);assert(element('#captchaTestBtn').disabled);
      assert(element('#captchaTestResult').textContent.length>0);
      assert.strictEqual(element('#captchaTestPreview').src,undefined);
    }
  });
  await test('PNG/JPEG/WebP 与 2MB 边界可预览，取消选择清空',async()=>{
    for(const type of ['image/png','image/jpeg','image/webp']){
      selectImage({type,size:2*1024*1024},'data:'+type+';base64,YWJjZA==');
      assert(!element('#captchaTestBtn').disabled);
    }
    selectImage(null);assert(element('#captchaTestBtn').disabled);
    assert.strictEqual(element('#captchaTestPreview').src,undefined);assert.strictEqual(element('#captchaTestResult').textContent,'');
  });
  await test('FileReader 竞态下旧图及旧错误不能覆盖新图',async()=>{
    selectImage({type:'image/png',size:10},null);const old=readers.at(-1);
    selectImage({type:'image/webp',size:10},null);const latest=readers.at(-1);assert(old.aborted);
    const newImage='data:image/webp;base64,YWJjZA==';latest.complete(newImage);
    old.complete(sampleImage);old.fail();
    assert.strictEqual(element('#captchaTestPreview').src,newImage);
    assert.strictEqual(element('#captchaTestResult').textContent,'');assert(!element('#captchaTestBtn').disabled);
  });
  await test('读取错误、恶意数据 URL 与同步异常禁用识别',async()=>{
    selectImage({type:'image/png',size:10},null);readers.at(-1).fail();
    assert(element('#captchaTestResult').textContent.includes('读取失败'));assert(element('#captchaTestBtn').disabled);
    for(const data of ['javascript:alert(1)','data:image/svg+xml;base64,YWJjZA==',sampleImage+'<img>']){
      selectImage({type:'image/png',size:10},data);assert(element('#captchaTestBtn').disabled);
      assert.strictEqual(element('#captchaTestPreview').src,undefined);
    }
    context.FileReader=class{readAsDataURL(){throw new Error('mock file failure')}abort(){}};
    selectImage({type:'image/png',size:10},null);assert(element('#captchaTestResult').textContent.includes('mock file failure'));
    assert(element('#captchaTestBtn').disabled);context.FileReader=MockFileReader;
  });
  await test('无有效图片、无通道或字符数越界不调用识别',async()=>{
    let sent=0;context.api=async()=>{sent++;return {}};configureAI();
    selectImage(null);await run('testCaptchaRecognition()');assert(element('#captchaTestResult').textContent.includes('有效图片'));
    selectImage();configureAI(false,false);element('#captchaTestLength').value='0';
    await run('testCaptchaRecognition()');assert(element('#captchaTestResult').textContent.includes('至少启用'));
    configureAI();
    for(const length of ['-1','17','1.5','bad']){
      element('#captchaTestLength').value=length;await run('testCaptchaRecognition()');
      assert(element('#captchaTestResult').textContent.includes('0 到 16'));assert(!element('#captchaTestBtn').disabled);
    }
    assert.strictEqual(sent,0);
  });
  await test('验证码请求携带当前配置、图片和可选字符数',async()=>{
    selectImage();configureAI(false,true,' ');let request;
    context.api=async(p,o)=>{request=[p,o];return {ok:true,text:'aB42',channel:'ocr',errors:[],elapsed_ms:48}};
    for(const length of ['', '0', '16']){
      element('#captchaTestLength').value=length;await run('testCaptchaRecognition()');
      assert.strictEqual(request[0],'/api/captcha/test');assert.strictEqual(request[1].method,'POST');
      assert.deepStrictEqual(JSON.parse(request[1].body),{image:sampleImage,length:Number(length),ai_base:'https://dummy.invalid/v1',ai_key:'',ai_model:'dummy-model',ai_enabled:'0',ocr_enabled:'1'});
      const out=element('#captchaTestResult').textContent;
      assert(out.includes('识别成功')&&out.includes('aB42')&&out.includes('本地 OCR')&&out.includes('48 ms'));
      assert(!element('#captchaTestBtn').disabled);
    }
    configureAI(true,false);element('#captchaTestLength').value='4';
    context.api=async(p,o)=>{request=JSON.parse(o.body);return {ok:true,text:'1234',channel:'ai',elapsed_ms:5}};
    await run('testCaptchaRecognition()');assert.strictEqual(request.ai_enabled,'1');assert.strictEqual(request.ocr_enabled,'0');
    assert(element('#captchaTestResult').textContent.includes('使用通道：AI'));
  });
  await test('AI 失败但 OCR 回退成功保留通道提醒与实际文本',async()=>{
    configureAI();context.api=async()=>({ok:true,text:'<img onerror=alert(1)>',channel:'ocr',errors:[{channel:'ai',message:'AI 超时 <script>'}],elapsed_ms:73});
    await run('testCaptchaRecognition()');const out=element('#captchaTestResult').textContent;
    assert(out.includes('识别成功')&&out.includes('通道提醒')&&out.includes('本地 OCR'));
    assert(out.includes('<img onerror=alert(1)>')&&out.includes('AI 超时 <script>')&&out.includes('73 ms'));
    assert(!element('#captchaTestBtn').disabled);
  });
  await test('识别失败展示所有错误、通道与实际非空文本',async()=>{
    context.api=async()=>({ok:false,text:'bad-length',channel:'none',errors:[{channel:'ai',message:'模型失败'},{channel:'ocr',message:'长度不符'}],elapsed_ms:21});
    await run('testCaptchaRecognition()');const out=element('#captchaTestResult').textContent;
    for(const text of ['识别失败','bad-length','使用通道：无','模型失败','长度不符','21 ms'])assert(out.includes(text));
    assert(!element('#captchaTestBtn').disabled);
  });
  await test('未知通道、缺失或非字符串识别结果可读且不会伪报成功',async()=>{
    for(const data of [{},{ok:true,text:''},{ok:true,text:123,errors:[null]},{ok:false,text:'other',channel:'unknown',errors:'invalid'}]){
      context.api=async()=>data;await run('testCaptchaRecognition()');const out=element('#captchaTestResult').textContent;
      assert(out.includes('识别失败')&&out.includes('未知')&&out.includes('失败原因'));
      assert(!element('#captchaTestBtn').disabled);
    }
  });
  await test('识别忙时防重复，失败恢复并展示现有错误提示',async()=>{
    let reject,sent=0;context.api=()=>{sent++;return new Promise((_,r)=>{reject=r})};
    const pending=run('testCaptchaRecognition()');assert(element('#captchaTestBtn').disabled);
    await run('testCaptchaRecognition()');assert.strictEqual(sent,1);reject(new Error('mock OCR unavailable'));await pending;
    assert(!element('#captchaTestBtn').disabled);assert(element('#captchaTestResult').textContent.includes('mock OCR unavailable'));
    assert.deepStrictEqual(calls.at(-1),['toast','mock OCR unavailable',1]);
  });
  await test('识别过程中换图忽略旧响应且完成后可识别新图',async()=>{
    let resolve;context.api=()=>new Promise(r=>{resolve=r});const pending=run('testCaptchaRecognition()');
    const next='data:image/jpeg;base64,YWJjZA==';selectImage({type:'image/jpeg',size:20},next);
    assert(element('#captchaTestBtn').disabled);assert.strictEqual(element('#captchaTestResult').textContent,'');
    resolve({ok:true,text:'OLD',channel:'ai'});await pending;
    assert(!element('#captchaTestBtn').disabled);assert.strictEqual(element('#captchaTestResult').textContent,'');
    let image;context.api=async(p,o)=>{image=JSON.parse(o.body).image;return {ok:true,text:'NEW',channel:'ai'}};
    await run('testCaptchaRecognition()');assert.strictEqual(image,next);assert(element('#captchaTestResult').textContent.includes('NEW'));
  });
  await test('识别过程中取消或换成无效图片不会恢复错误按钮状态',async()=>{
    for(const file of [null,{type:'text/plain',size:20}]){
      selectImage();let reject;context.api=()=>new Promise((_,r)=>{reject=r});const pending=run('testCaptchaRecognition()');
      selectImage(file,null);const out=element('#captchaTestResult').textContent;
      reject(new Error('stale error'));await pending;assert(element('#captchaTestBtn').disabled);
      assert.strictEqual(element('#captchaTestResult').textContent,out);
    }
  });
  await test('真实 api 函数的 HTTP 错误使用模拟 fetch 显示原因与登录提示',async()=>{
    let gated=0;
    const apiContext=vm.createContext({fetch:async()=>({status:400,ok:false,json:async()=>({error:'mock HTTP 参数错误'})}),showLoginGate(){gated++}});
    vm.runInContext(slice('async function api(','const esc='),apiContext);
    context.api=apiContext.api;await run('testAIConnection()');
    assert(element('#aiTestResult').textContent.includes('mock HTTP 参数错误'));assert(!element('#aiTestBtn').disabled);
    selectImage();await run('testCaptchaRecognition()');
    assert(element('#captchaTestResult').textContent.includes('mock HTTP 参数错误'));assert(!element('#captchaTestBtn').disabled);
    apiContext.fetch=async()=>({status:401});await run('testAIConnection()');assert.strictEqual(gated,1);
    assert(element('#aiTestResult').textContent.includes('未登录面板'));assert(!element('#aiTestBtn').disabled);
  });
  await test('签到日志显示来源和失败原因且不执行服务端文本',async()=>{
    context.LOG_KINDS={signin:'签到'};context.fmt=()=>'';
    run(slice('function logRow(h){','let logQ='));
    context.logFixture={action:'signin',ok:0,ts:1,chat_id:'@dummy',detail:{task:'fixture',command:'/start',error:'<img src=x onerror=bad()>',captcha_message_id:102,captcha_prompt:'请输入测试信息',command_delay:0,button_delay:3,refresh_buttons:0}};
    const markup=run('logRow(logFixture)');
    assert(markup.includes('失败原因'));assert(markup.includes('图片来源'));assert(markup.includes('消息 #102'));
    assert(markup.includes('命令后等待'));assert(markup.includes('0 秒'));assert(markup.includes('复用同一按钮'));
    assert(markup.includes('&lt;img'));assert(!markup.includes('<img'));
  });
  console.log(`前端回归 ${count} 项全部通过`);
})().catch(e=>{console.error(e);process.exitCode=1});
