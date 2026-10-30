'use strict';
// 原前端代码提取后运行；不访问服务器，不依赖第三方 DOM 库。
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(path.join(__dirname,'../web/index.html'),'utf8');
const js=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const elements=new Map(),calls=[],intervals=new Map(),readers=[];let intervalId=0,nodeId=0;
class MockFileReader{
  constructor(){readers.push(this)}
  readAsDataURL(file){this.file=file}
  readAsText(file){this.file=file}
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
    const classes=new Set();
    const el={innerHTML:'',textContent:'',children:[],style:{},dataset:{},disabled:false,src:'',listeners:{},attributes:{},querySelector(){return null},querySelectorAll(){return []},getAttribute(k){return this.attributes[k]??null},setAttribute(k,v){this.attributes[k]=String(v)},click(){this.clicked=true},remove(){this.removed=true},classList:{add(...v){v.forEach(k=>classes.add(k))},remove(...v){v.forEach(k=>classes.delete(k))},contains(k){return classes.has(k)},toggle(k,force){const add=force===undefined?!classes.has(k):force;add?classes.add(k):classes.delete(k);return add}},scrollIntoView(){},addEventListener(t,fn){this.listeners[t]=fn},contains(){return true},removeAttribute(k){delete this[k]},appendChild(node){this.children.push(node);return node},replaceChildren(...nodes){this.children=nodes;this.textContent=''}};
    let value='';Object.defineProperty(el,'value',{get(){return value},set(v){value=String(v)}});
    elements.set(id,el);
  }
  return elements.get(id);
}
const context=vm.createContext({console,Buffer,Date,JSON,String,Error,Promise,URL,FileReader:MockFileReader,
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
run(slice('// ---------- 账号请求范围 ----------','async function api('));
run(slice('// ---------- 安全图标与共享图标库 ----------','let autos=[], editAuto=null, editAutoAccount=null;'));
run(slice('// ---------- 可视化步骤编辑器 ----------','const autoGroupSel='));
run(slice('let allGroups=[];','async function loadDialogs'));
run(slice('function autoDesc(a){','function dupAuto'));
run('let autos=[],editAuto=null,editAutoAccount=null;');
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
  await test('结果等待表单包含默认值范围步进及持续轮询语义',async()=>{
    const form=html.slice(html.indexOf('id="af-signin"'),html.indexOf('id="af-grabcodes"'));
    for(const [id,label,value,min,max,step] of [['sResultDelay','结果判断前延迟（秒）','0','0','120','0.1'],['sResultTimeout','等待最终回复上限（秒）','30','1','300','1']]){
      assert(form.includes('<label for="'+id+'">'+label+'</label>'));
      const input=form.match(new RegExp('<input\\b[^>]*id="'+id+'"[^>]*>'))[0];
      for(const attr of ['type="number"','value="'+value+'"','min="'+min+'"','max="'+max+'"','step="'+step+'"','required'])assert(input.includes(attr));
    }
    for(const text of ['按钮/命令完成或验证提交后','先等待结果判断前延迟','持续轮询最终回复','不是等一次就判断','本日不自动重发'])assert(form.includes(text));
  });
  await test('结果等待新建及关闭取消恢复零秒和三十秒默认值',async()=>{
    for(const reset of ["cancelAutoEdit('signin')","closeAutoForm('signin');openAutoForm('signin')"]){
      editSignin({result_delay:9.2,result_timeout:123});run(reset);
      assert.strictEqual(element('#sResultDelay').value,'0');assert.strictEqual(element('#sResultTimeout').value,'30');assert.strictEqual(run('editAuto'),null);
    }
  });
  await test('旧任务结果等待缺失或 null 独立回填不影响已有配置',async()=>{
    for(const data of [{},{result_delay:null,result_timeout:null},{result_delay:0},{result_delay:null,result_timeout:1},{result_delay:120,result_timeout:null}]){
      editSignin(data);assert.strictEqual(element('#sResultDelay').value,String(data.result_delay??0));assert.strictEqual(element('#sResultTimeout').value,String(data.result_timeout??30));
    }
  });
  await test('结果等待编辑保留数值零字符串零及合法上下边界',async()=>{
    for(const [result_delay,result_timeout] of [[0,1],['0','300'],[0.1,30],[120,300],[0,0],['0','0']]){
      editSignin({result_delay,result_timeout});assert.strictEqual(element('#sResultDelay').value,String(result_delay));assert.strictEqual(element('#sResultTimeout').value,String(result_timeout));
    }
  });
  await test('结果等待新增保存数值默认值且空白输入独立回退',async()=>{
    for(const fields of [{},{'#sResultDelay':' ','#sResultTimeout':''}]){
      prepareSignin(fields);let request;context.api=async(p,o)=>{request=[p,o];return {}};await run("saveAuto('signin')");
      assert.strictEqual(request[0],'/api/autos');assert.strictEqual(request[1].method,'POST');const body=JSON.parse(request[1].body);
      assert.strictEqual(body.result_delay,0);assert.strictEqual(body.result_timeout,30);
    }
  });
  await test('结果等待 POST PUT 保留完整签到参数零次重试及边界',async()=>{
    for(const editing of [false,true])for(const [delay,timeout] of [[0,1],[120,300],[0.1,30]]){
      editing?editSignin({enabled:0}):prepareSignin();
      const values={'#sName':'完整任务','#sTarget':'@dummy_bot','#sCmd':'/checkin','#sIconUrl':'https://dummy.invalid/icon.png','#sWinStart':'09:00','#sWinEnd':'10:00','#sCommandDelay':'0','#sButtonDelay':'2.7','#sResultDelay':delay,'#sResultTimeout':timeout,'#sBtnText':'签到','#sBtnEmoji':'✅','#sClicks':'3','#sClickGap':'500','#sRefreshButtons':'0','#sKw':'成功','#sFw':'明确失败','#sPts':'积分:(\\d+)','#sRetries':'0'};
      for(const [id,value] of Object.entries(values))element(id).value=value;
      for(const id of ['#sIsChat','#sClickBtn','#sCap'])element(id).checked=true;
      let request;context.api=async(p,o)=>{request=[p,o];return {}};await run("saveAuto('signin')");
      assert.strictEqual(request[0],editing?'/api/autos/42':'/api/autos');assert.strictEqual(request[1].method,editing?'PUT':'POST');
      const expected={type:'signin',name:'完整任务',target:'@dummy_bot',command:'/checkin',icon_url:'https://dummy.invalid/icon.png',is_chat:1,win_start:'09:00',win_end:'10:00',click_btn:1,btn_text:'签到',btn_emoji:'✅',click_times:3,click_interval:500,refresh_buttons:0,command_delay:0,button_delay:2.7,result_delay:delay,result_timeout:timeout,success_kw:'成功',fail_kw:'明确失败',points_pat:'积分:(\\d+)',retries:0,signin_captcha:1};
      if(editing)expected.enabled=0;assert.deepStrictEqual(JSON.parse(request[1].body),expected);
    }
  });
  await test('结果等待拒绝越界非有限数及错误步进且不发送请求',async()=>{
    let sent=0;context.api=async()=>{sent++;return {}};
    for(const [id,invalid,hint] of [['#sResultDelay',['-0.1','120.1','NaN','Infinity','1.05'],'0–120 秒'],['#sResultTimeout',['0','-1','301','NaN','Infinity','1.1'],'1–300 秒']]){
      for(const value of invalid){prepareSignin({[id]:value});await run("saveAuto('signin')");assert(calls.at(-1)[1].includes(hint));}
    }
    assert.strictEqual(sent,0);
  });
  await test('结果等待任务摘要显示缺省零边界且安全转义',async()=>{
    for(const values of [{},{result_delay:null,result_timeout:null},{result_delay:0,result_timeout:1},{result_delay:120,result_timeout:300}]){
      context.autoInput={type:'signin',...values};const desc=run('autoDesc(autoInput)');
      assert(desc.includes('结果判断前延迟 '+(values.result_delay??0)+'秒'));assert(desc.includes('等待最终回复上限 '+(values.result_timeout??30)+'秒'));
    }
    context.autoInput={type:'signin',result_delay:'<img src=x>',result_timeout:'<script>bad()</script>'};const desc=run('autoDesc(autoInput)');assert(desc.includes('&lt;img')&&desc.includes('&lt;script'));assert(!desc.includes('<img')&&!desc.includes('<script'));
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
    vm.runInContext(slice('// ---------- 账号请求范围 ----------','const esc='),apiContext);
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
    vm.runInContext(slice('// ---------- 账号请求范围 ----------','const esc='),apiContext);
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
  await test('签到日志展示结果等待零边界且兼容缺少新字段',async()=>{
    for(const values of [{result_delay:0,result_timeout:1},{result_delay:120,result_timeout:300}]){
      context.logFixture={action:'signin',ok:1,ts:1,detail:{...values}};const markup=run('logRow(logFixture)');
      for(const [label,value] of [['结果判断前延迟',values.result_delay],['等待最终回复上限',values.result_timeout]])assert(markup.includes('<span class="k">'+label+'</span>'+value+' 秒'));
    }
    context.logFixture={action:'signin',ok:1,ts:1,detail:{}};const old=run('logRow(logFixture)');assert(!old.includes('结果判断前延迟')&&!old.includes('等待最终回复上限'));assert(old.includes('成功'));
    context.logFixture.detail={result_delay:'<img src=x>',result_timeout:'<script>bad()</script>'};const safe=run('logRow(logFixture)');assert(safe.includes('&lt;img')&&safe.includes('&lt;script'));assert(!safe.includes('<img')&&!safe.includes('<script'));
  });
  await test('待确认日志优先识别状态展示原因且不误报失败',async()=>{
    for(const ok of [false,0,null,undefined,true])for(const reason of [{reason:'<img src=x onerror=bad()>'},{error:'等待超时'},{}]){
      context.logFixture={action:'signin',ok,ts:1,detail:{status:'unconfirmed',...reason}};const markup=run('logRow(logFixture)');
      assert(markup.includes('class="st pending">待确认'));assert(markup.includes('待确认原因'));assert(markup.includes('本日不自动重发'));
      assert(!markup.includes('>失败<')&&!markup.includes('失败原因')&&!markup.includes('<img'));assert(markup.includes(reason.reason?'&lt;img':reason.error||'等待最终回复超时，结果仍未知'));
    }
    for(const [ok,label] of [[true,'成功'],[false,'失败'],[null,'记录']]){context.logFixture={action:'signin',ok,ts:1,detail:{}};assert(run('logRow(logFixture)').includes('>'+label+'</span>'));}
  });
  // 整份真实前端脚本在独立离线 VM 中运行，避免仅测试复制实现。
  const fullCalls=[],downloaded=[],timers=[],store=new Map(),sessionStore=new Map();
  const q=id=>element('#full-'+id);
  const fullDocument={visibilityState:'visible',listeners:{},addEventListener(t,fn){this.listeners[t]=fn},querySelector:q,querySelectorAll:()=>[],getElementById:id=>q('#'+id),documentElement:{dataset:{}},body:{style:{},appendChild(n){downloaded.push(n);return n}},createElement(){const node=q('node-'+(++nodeId));Object.defineProperty(node,'innerHTML',{get(){return ''},set(){throw new Error('新功能禁止不可信 HTML sink')}});return node}};
  const app=vm.createContext({console,FileReader:MockFileReader,Blob,URL:Object.assign(class extends URL {},{createObjectURL(blob){fullCalls.push(['blob',blob]);return 'blob:mock-config'},revokeObjectURL(url){fullCalls.push(['revoke',url])}}),document:fullDocument,window:{confirm(message){fullCalls.push(['confirm',message]);return false}},confirm:()=>false,sessionStorage:{getItem:k=>sessionStore.get(k)||null,setItem:(k,v)=>sessionStore.set(k,String(v))},localStorage:{getItem:k=>store.get(k)||null,setItem:(k,v)=>store.set(k,String(v))},fetch(){throw new Error('离线测试禁止真实网络')},setTimeout(fn){timers.push(fn);return timers.length},clearTimeout(){},setInterval(){return 1},clearInterval(){},btoa:s=>Buffer.from(s,'binary').toString('base64'),unescape,encodeURIComponent});
  const ex=s=>vm.runInContext(s,app),copy=x=>JSON.parse(JSON.stringify(x));
  ex(js.replace(/\nbootstrap\(\);\s*$/,''));
  const realApi=app.api, realLogLoader=app.loadLogs;
  const mockConfig=(name='纯配置')=>({format:'tg-persona-config',schema_version:1,account:{id:'default',label:'默认'},automations:[{type:'custom',name,target:'@dummy',custom_steps:'[{"t":"send","v":"/start"}]',enabled:1}],rules:[],watched_groups:[]});
  function useConfig(data=mockConfig()){q('#configFile').files=[{name:'backup.json',type:'application/json',size:1024}];ex('readConfigFile()');readers.at(-1).complete(JSON.stringify(data))}
  function resetBatch(kind='window'){ex('batchBusy=false;selectedAutoIds=new Set([1,2]);invalidateBatchPreview()');q('#batchKind').value=kind;q('#batchStart').value='08:00';q('#batchEnd').value='12:00';q('#batchEvery').value='1';q('#batchUnit').value='60'}
  await test('完整脚本可离线初始化，JSON高级折叠且没有新增验证码入口',async()=>{
    assert(html.includes('<details id="stepAdvanced"'));assert(!html.includes("fillStep('captcha')"));
    assert(html.includes('人工验证等待'));assert(html.includes('最多 50 步'));
  });
  await test('旧步骤所有未知字段、captcha及字符串数值无损往返',async()=>{
    const old=[{t:'send',v:'/start',extra:{nested:[0,false]}},{t:'wait',v:'0000',legacy:1},{t:'click',v:'',any:true,ai:'legacy-value',extra:'x'},{t:'captcha',v:'4',send:'',other:{x:7}},{t:'until',success:'成功',fail:'失败',timeout:'0',legacy:false},{t:'future',payload:[1,2]},null];
    app.fixture=old;ex('setVisualSteps(JSON.stringify(fixture));syncStepJSON()');assert.deepStrictEqual(JSON.parse(q('#cSteps').value),old);
    const titles=q('#stepCards').children.map(c=>c.children[0].textContent);assert(titles.includes('4. 旧版验证步骤（保留）'));
    ex("fillStep('captcha')");assert.deepStrictEqual(JSON.parse(q('#cSteps').value),old);
  });
  await test('步骤排序、删除、零秒及毫秒转换实时写回JSON',async()=>{
    ex("setVisualSteps('[{\"t\":\"wait\",\"v\":\"1000\"},{\"t\":\"send\",\"v\":\"/start\"}]')");
    const input=q('#stepCards').children[0].children[1].children[0];input.value='0';input.listeners.input();assert.strictEqual(JSON.parse(q('#cSteps').value)[0].v,'0');
    input.value='0.125';input.listeners.input();assert.strictEqual(JSON.parse(q('#cSteps').value)[0].v,'125');
    ex('moveStep(0,1)');assert.strictEqual(JSON.parse(q('#cSteps').value)[1].t,'wait');ex('removeStep(0)');assert.strictEqual(JSON.parse(q('#cSteps').value).length,1);
    ex('moveStep(0,-1)');assert.strictEqual(JSON.parse(q('#cSteps').value)[0].v,'125');
  });
  await test('模板替换有确认，人工验证仅until且等待零不回退默认',async()=>{
    q('#stepTemplate').value='manual';const before=q('#cSteps').value;ex('applyStepTemplate()');assert.strictEqual(q('#cSteps').value,before);
    app.window.confirm=()=>true;ex('applyStepTemplate()');const list=JSON.parse(q('#cSteps').value);assert.deepStrictEqual(list.map(s=>s.t),['send','until']);
    assert(!list.some(s=>s.t==='captcha'));ex('visualSteps[1].timeout=0;syncStepJSON()');assert.strictEqual(JSON.parse(q('#cSteps').value)[1].timeout,0);
    q('#stepTemplate').value='basic';ex('applyStepTemplate()');assert.deepStrictEqual(JSON.parse(q('#cSteps').value).map(s=>s.t),['send','wait','click','until']);
  });
  await test('步骤上限、非法时间与空发送用人话拒绝保存',async()=>{
    ex("setVisualSteps(JSON.stringify(Array.from({length:50},()=>({t:'wait',v:'0'}))));fillStep('wait')");assert.strictEqual(JSON.parse(q('#cSteps').value).length,50);
    assert(ex("stepIssue([{t:'wait',v:-1}],true)").includes('等待时间'));assert(ex("stepIssue([{t:'send',v:' '}],true)").includes('发送的消息'));
    assert.throws(()=>ex("parseSteps(JSON.stringify(Array.from({length:51},()=>({t:'wait',v:0}))))"),/50/);
    q('#cName').value='零等待';q('#cTarget').value='@dummy';q('#cRetry').value='1';ex("setVisualSteps('[{\"t\":\"wait\",\"v\":0}]');editAuto=null;editAutoAccount=accountContext()");let body;
    app.api=async(p,o)=>{body=JSON.parse(o.body);return {}};app.loadAutos=async()=>{};await ex("saveAuto('custom')");assert.strictEqual(JSON.parse(body.custom_steps)[0].v,0);
  });
  await test('批量全选取消计数跨各任务类型且间隔单位转秒',async()=>{
    ex("autos=[{id:1,type:'signin'},{id:2,type:'keepalive'},{id:3,type:'custom'}];selectAllAutos(true)");assert.strictEqual(q('#autoSelectedCount').textContent,'已选 3 项');assert(q('#autoSelectAll').checked);
    ex('toggleAutoSelection(2,false)');assert.strictEqual(q('#autoSelectedCount').textContent,'已选 2 项');assert(q('#autoSelectAll').indeterminate);
    ex('selectAllAutos(false)');assert.strictEqual(ex('selectedAutoIds.size'),0);
    resetBatch('interval');for(const [unit,value,seconds] of [['60','2',120],['3600','1.5',5400],['86400','1',86400],['60','0',0]]){q('#batchUnit').value=unit;q('#batchEvery').value=value;assert.strictEqual(ex('buildBatchRequest().patch.poll_interval'),seconds)}
    q('#batchEvery').value='-1';assert.throws(()=>ex('buildBatchRequest()'),/间隔/);resetBatch();q('#batchStart').value='25:00';assert.throws(()=>ex('buildBatchRequest()'),/时间/);
  });
  await test('批量JSON预览再确认提交并安全显示skip',async()=>{
    resetBatch();let requests=[];app.api=async(p,o)=>{requests.push([p,JSON.parse(o.body)]);return {ok:true,updated:[{id:1,name:'签到'}],updated_count:1,skipped:[{id:2,name:'<img src=x>',reason:'该类型不支持'}]}};
    await ex('previewBatch()');assert.strictEqual(requests[0][0],'/api/autos/batch');assert.deepStrictEqual(requests[0][1],{ids:[1,2],action:'update',patch:{win_start:'08:00',win_end:'12:00'},preview:true});
    assert(q('#batchResult').textContent.includes('跳过 <img src=x>'));assert(!q('#batchApplyBtn').disabled);
    app.window.confirm=()=>false;await ex('applyBatch()');assert.strictEqual(requests.length,1);
    app.window.confirm=()=>true;await ex('applyBatch()');assert.strictEqual(requests[1][1].preview,false);assert.deepStrictEqual(requests[1][1].patch,requests[0][1].patch);
  });
  await test('删除启停需要预览确认，改选项使旧预览失效',async()=>{
    for(const action of ['delete','enable','disable']){resetBatch(action);let req=[];app.api=async(p,o)=>{req.push(JSON.parse(o.body));return {updated:[{id:1,name:'A'}],skipped:[]}};await ex('previewBatch()');assert.deepStrictEqual(req[0],{ids:[1,2],action,patch:{},preview:true});app.window.confirm=message=>{assert(action!=='delete'||message.includes('不可恢复'));return false};await ex('applyBatch()');assert.strictEqual(req.length,1)}
    resetBatch();app.api=async()=>({updated:[{id:1}]});await ex('previewBatch()');q('#batchEnd').value='14:00';app.window.confirm=()=>{throw new Error('选项变化不应确认')};await ex('applyBatch()');assert(q('#batchApplyBtn').disabled);
  });
  await test('批量失败释放按钮且账号切换忽略旧预览',async()=>{
    resetBatch();app.api=async()=>{throw new Error('mock批量冲突')};await ex('previewBatch()');assert(q('#batchResult').textContent.includes('mock批量冲突'));assert(!q('#batchPreviewBtn').disabled);
    let done;app.api=()=>new Promise(r=>done=r);const pending=ex('previewBatch()');ex("selectedAccount='second';accountEpoch++;batchBusy=false;invalidateBatchPreview()");done({updated:[{id:1,name:'旧任务'}]});await pending;assert.strictEqual(ex('batchPreview'),null);assert(!q('#batchResult').textContent.includes('旧任务'));ex("selectedAccount='default';accountEpoch++");
  });
  await test('导出全部与选中任务使用Blob下载且无秘密历史字段',async()=>{
    const data=mockConfig();let req=[];app.api=async(p,o)=>{req.push([p,o]);return data};ex('selectedAutoIds=new Set([1,2])');await ex('exportConfig(false)');await ex('exportConfig(true)');assert.strictEqual(req[0][0],'/api/tasks/export');assert.strictEqual(req[0][1].method,undefined);assert.deepStrictEqual(JSON.parse(req[1][1].body),{ids:[1,2]});
    const blob=fullCalls.filter(c=>c[0]==='blob').at(-1)[1];assert.deepStrictEqual(JSON.parse(await blob.text()),data);assert(downloaded.at(-1).clicked);assert(downloaded.at(-1).removed);
    for(const field of ['api_key','api_hash','api_id','phone','session','history','last_at','ai_key']){app.unsafe={...data,[field]:'should-not-leave'};assert.throws(()=>ex('assertSafeConfig(unsafe)'),/拒绝处理/)}
    const n=downloaded.length;app.api=async()=>({...data,session:'mock-session'});await ex('exportConfig(false)');assert.strictEqual(downloaded.length,n);assert(!q('#configExportAll').disabled);
  });
  await test('导入文件大小空文件后缀及格式限制，只读取不上传',async()=>{
    let sent=0;app.api=async()=>{sent++;return {}};
    for(const file of [{name:'x.json',size:0},{name:'x.json',size:2*1024*1024+1},{name:'x.txt',size:1}]){q('#configFile').files=[file];ex('readConfigFile()');assert(q('#configPreviewBtn').disabled)}
    q('#configFile').files=[{name:'x.json',size:2*1024*1024}];ex('readConfigFile()');readers.at(-1).complete(JSON.stringify(mockConfig()));assert(!q('#configPreviewBtn').disabled);assert.strictEqual(sent,0);
    useConfig({...mockConfig(),phone:'mock-phone'});assert(q('#configPreviewBtn').disabled);assert(!q('#configResult').textContent.includes('mock-phone'));
    q('#configFile').files=[{name:'x.json',size:16}];ex('readConfigFile()');readers.at(-1).complete('{broken');assert(q('#configResult').textContent.includes('无法读取'));
  });
  await test('文件读取竞态与取消：旧文件成功错误不能覆盖新文件',async()=>{
    q('#configFile').files=[{name:'a.json',size:10}];ex('readConfigFile()');const a=readers.at(-1);
    useConfig(mockConfig('新文件'));a.complete(JSON.stringify(mockConfig('旧文件')));a.fail();assert.strictEqual(ex('configData.automations[0].name'),'新文件');assert(!q('#configPreviewBtn').disabled);
    ex('cancelConfigImport()');a.complete(JSON.stringify(mockConfig('迟到')));assert.strictEqual(ex('configData'),null);assert(q('#configApplyBtn').disabled);assert.strictEqual(q('#configMode').value,'merge');
  });
  await test('导入默认追加activate false，预览counts警告名称后确认才应用',async()=>{
    ex('cancelConfigImport()');useConfig();const req=[];app.api=async(p,o)=>{req.push(JSON.parse(o.body));return {ok:true,preview:true,counts:{tasks:1,rules:0,groups:0,duplicates:0},tasks:[{name:'<img src=x>',type:'custom'}],warnings:['mock提醒']}};
    await ex('previewImport()');assert.strictEqual(req[0].mode,'merge');assert.strictEqual(req[0].activate,false);assert.strictEqual(req[0].include_rules,true);assert.strictEqual(req[0].include_groups,true);assert.strictEqual(req[0].preview,true);
    assert(q('#configResult').textContent.includes('<img src=x>'));assert(q('#configResult').textContent.includes('mock提醒'));app.window.confirm=()=>false;await ex('applyImport()');assert.strictEqual(req.length,1);
    app.window.confirm=()=>true;app.loadRules=app.loadRuleGroups=app.loadDialogs=()=>{};await ex('applyImport()');assert.strictEqual(req[1].preview,false);assert.deepStrictEqual({...req[1],preview:true},req[0]);
  });
  await test('覆盖导入两次强确认且取消无写入，activate true仅显式勾选',async()=>{
    ex('cancelConfigImport()');useConfig();q('#configMode').value='replace';q('#configActivate').checked=true;ex('invalidateImportPreview()');const req=[];app.api=async(p,o)=>{req.push(JSON.parse(o.body));return {counts:{tasks:1},tasks:[],warnings:[]}};await ex('previewImport()');assert.strictEqual(req[0].activate,true);
    let confirmations=0;app.window.confirm=message=>{confirmations++;assert(message.includes('恢复')||message.includes('执行'));return confirmations===1};await ex('applyImport()');assert.strictEqual(confirmations,2);assert.strictEqual(req.length,1);
    app.window.confirm=()=>true;await ex('applyImport()');assert.strictEqual(req[1].mode,'replace');assert.strictEqual(req[1].preview,false);
  });
  await test('导入预览换文件换选项取消及换账号全部忽略旧结果',async()=>{
    for(const change of ['file','mode','cancel','account']){
      ex('cancelConfigImport()');useConfig();let resolve;app.api=()=>new Promise(r=>resolve=r);const pending=ex('previewImport()');
      if(change==='file')useConfig(mockConfig('新版'));if(change==='mode'){q('#configMode').value='replace';ex('invalidateImportPreview()')};if(change==='cancel')ex('cancelConfigImport()');if(change==='account')ex("selectedAccount='new';accountEpoch++;cancelConfigImport()");
      resolve({counts:{tasks:9},tasks:[{name:'迟到旧预览'}],warnings:[]});await pending;assert.strictEqual(ex('configPreview'),null);assert(!q('#configResult').textContent.includes('迟到旧预览'));assert(q('#configApplyBtn').disabled);assert.strictEqual(ex('configBusy'),false);if(change==='account')ex("selectedAccount='default';accountEpoch++");
    }
    useConfig();app.api=async()=>{throw new Error('mock导入错误')};await ex('previewImport()');assert(!q('#configPreviewBtn').disabled);assert(q('#configResult').textContent.includes('mock导入错误'));
  });
  await test('真实API对账号范围加头，面板账户管理更新备份不依赖选择',async()=>{
    ex("selectedAccount='profile-a';accountEpoch++");const req=[];app.fetch=async(p,o)=>{req.push([p,o]);return {ok:true,status:200,json:async()=>({ok:true})}};
    for(const route of ['/api/autos','/api/settings','/api/rules','/api/watched','/api/dialogs','/api/logs','/api/state','/api/tg/send_code','/api/tasks/import'])await realApi(route,{method:'POST',body:'{}',headers:{Custom:'test'}});
    for(const [,o] of req){assert.strictEqual(o.headers['X-Account-ID'],'profile-a');assert.strictEqual(o.headers.Custom,'test');assert(!('_account' in o))}
    req.length=0;for(const route of ['/api/panel/login','/api/panel/password','/api/accounts','/api/accounts/other','/api/version?check=1','/api/update','/api/backups/cleanup'])await realApi(route);for(const [,o] of req)assert.strictEqual(o.headers['X-Account-ID'],undefined);
    ex("selectedAccount='default';accountEpoch++");
  });
  await test('请求冻结头部，账号A→B→A仍拒绝旧响应与旧错误且不弹toast',async()=>{
    const before=fullCalls.filter(c=>c[0]==='confirm').length;ex("selectedAccount='a';accountEpoch++");let resolve,options;app.fetch=(p,o)=>{options=o;return new Promise(r=>resolve=r)};const pending=realApi('/api/autos/42',{method:'PUT',body:'{"name":"a"}'}).catch(e=>e);
    ex("selectedAccount='b';accountEpoch++;selectedAccount='a';accountEpoch++");resolve({ok:true,status:200,json:async()=>({name:'旧账号'})});const error=await pending;assert(error.staleAccount);assert.strictEqual(error.message,'');assert.strictEqual(options.headers['X-Account-ID'],'a');assert.strictEqual(fullCalls.filter(c=>c[0]==='confirm').length,before);
    let reject;app.fetch=()=>new Promise((r,j)=>reject=j);const failed=realApi('/api/state').catch(e=>e);ex("selectedAccount='default';accountEpoch++");reject(new Error('旧账号网络失败'));assert((await failed).staleAccount);
  });
  await test('缓存清理含任务群规则日志勾选表单和凭证，不在localStorage保存秘密',async()=>{
    ex("autos=[{id:42,type:'custom'}];allGroups=[{chat_id:1}];allRules=[{id:42}];watchedGroups=[{chat_id:1}];selectedAutoIds=new Set([42]);editAuto={id:42};editingRule=42;tgPhase='ready';state={tg:{me:{name:'旧client'}},logged_in:true};logQ='old';ruleGroupSel.add('1');autoGroupSel.grabcodes.add('1')");
    q('#logList').innerHTML='旧日志';q('#sAiKey').value='mock-secret';q('#tgCard').innerHTML='旧手机号';ex('resetAccountCaches()');
    for(const name of ['autos','allGroups','allRules','watchedGroups'])assert.strictEqual(ex(name+'.length'),0);assert.strictEqual(ex('selectedAutoIds.size'),0);assert.strictEqual(ex('editAuto'),null);assert.strictEqual(ex('editingRule'),null);assert.strictEqual(ex('state.logged_in'),false);assert.strictEqual(q('#sAiKey').value,'');assert.strictEqual(q('#cSteps').value,'[]');assert.strictEqual(ex('logQ'),'');assert.strictEqual(q('#autoSelectedCount').textContent,'已选 0 项');
    const loaders=['loadState','loadSettings','loadAutos','loadRules','loadRuleGroups','loadDialogs','loadLogs','renderTgCard'];for(const name of loaders)app[name]=()=>{};
    ex("switchAccount('account-new')");assert.strictEqual(store.get('tgp_selected_id'),'account-new');assert(![...store.keys()].some(k=>/key|phone|session/.test(k)));
  });
  await test('状态、任务缓存和TG表单过期响应不覆盖当前账号',async()=>{
    const clean=vm.createContext({...app,fetch(){throw new Error('禁止网络')},localStorage:{getItem:()=>null,setItem(){}},document:fullDocument,window:{confirm:()=>false},setTimeout(){},clearTimeout(){}});vm.runInContext(js.replace(/\nbootstrap\(\);\s*$/,''),clean);const cx=s=>vm.runInContext(s,clean);
    for(const fn of ['loadState','loadAutos','renderTgCard']){let done;clean.api=()=>new Promise(r=>done=r);const pending=cx(fn+'()');cx("selectedAccount='other';accountEpoch++;state={tg:{},logged_in:false};autos=[]");done(fn==='loadState'?{tg:{me:{name:'迟到client'}},logged_in:true,stats:{}}:fn==='loadAutos'?{autos:[{id:42,name:'迟到任务'}]}:{api_id:123456,api_hash:'mock-old-hash'});await pending;assert.strictEqual(cx('state.logged_in'),false);assert.strictEqual(cx('autos.length'),0);assert(!q('#tgCard').innerHTML.includes('mock-old-hash'));cx("selectedAccount='default';accountEpoch++")}
  });
  await test('账号标签纯文本，默认不可删，其他账号两次确认，管理错误恢复按钮',async()=>{
    ex("accounts=[{id:'default',label:'<img src=x>',logged_in:false},{id:'work',label:'工作',logged_in:true}];selectedAccount='default';renderAccounts()");assert.strictEqual(q('#accountTop').children[0].textContent,'<img src=x> · 未登录');assert(q('#accountDelete').disabled);
    let sent=0;app.api=async()=>{sent++;return {}};await ex("manageAccount('delete')");assert.strictEqual(sent,0);
    ex("selectedAccount='work'");let prompts=0;app.window.confirm=()=>++prompts===1;await ex("manageAccount('delete')");assert.strictEqual(prompts,2);assert.strictEqual(sent,0);
    q('#accountLabel').value='重命名';app.api=async()=>{throw new Error('mock账号冲突')};await ex("manageAccount('rename')");assert(!q('#accountRename').disabled);assert(q('#accountResult').textContent.includes('mock账号冲突'));
  });
  await test('结构化detail及Pydantic数组提取人话，不输出input或对象',async()=>{
    for(const [detail,want] of [[{message:'当前账号任务不存在',ids:[42],input:'mock-secret'},'当前账号任务不存在'],[[{msg:'Field required',input:'mock-secret',loc:['body','name']}],'请补齐必填项'],[[{msg:'Input should be a valid integer',input:'mock-secret'}],'请填写有效的整数']]){
      app.fetch=async()=>({ok:false,status:422,json:async()=>({detail})});await assert.rejects(()=>realApi('/api/autos/batch'),e=>e.message.includes(want)&&!e.message.includes('mock-secret')&&!e.message.includes('[object Object]'));
    }
  });
  await test('旧账号同ID任务保存不写新账号，已发PUT不关新表单',async()=>{
    ex("selectedAccount='a';accountEpoch++;editAuto={id:42,type:'custom',enabled:1};editAutoAccount=accountContext()");q('#cName').value='任务A';q('#cTarget').value='@dummy';q('#cRetry').value='1';ex("setVisualSteps('[{\"t\":\"wait\",\"v\":0}]')");let done,request;
    app.api=(p,o)=>{request=[p,o];return new Promise(r=>done=r)};const pending=ex("saveAuto('custom')");assert.strictEqual(request[0],'/api/autos/42');assert.strictEqual(request[1]._account.id,'a');
    ex("selectedAccount='b';accountEpoch++;editAuto={id:42,type:'custom',enabled:0};editAutoAccount=accountContext()");q('#cName').value='任务B';done({ok:true});await pending;assert.strictEqual(q('#cName').value,'任务B');assert.strictEqual(ex('editAuto.enabled'),0);
    ex("editAutoAccount={id:'a',epoch:0}");let sent=0;app.api=async()=>{sent++;return {}};await ex("saveAuto('custom')");assert.strictEqual(sent,0);
  });
  await test('文件读取期间修改导入模式保留最新文件，需重新预览',async()=>{
    ex('cancelConfigImport()');q('#configFile').files=[{name:'latest.json',size:128}];ex('readConfigFile()');const reader=readers.at(-1);q('#configMode').value='replace';ex('invalidateImportPreview()');reader.complete(JSON.stringify(mockConfig('最新配置')));
    assert.strictEqual(ex('configData.automations[0].name'),'最新配置');assert.strictEqual(ex('buildImportRequest().mode'),'replace');assert(!q('#configPreviewBtn').disabled);assert(q('#configApplyBtn').disabled);
  });
  await test('签到窗口时区改变后旧批量预览必须重新确认',async()=>{
    resetBatch();q('#sTz').value='UTC';let sent=0;app.api=async()=>{sent++;return {updated:[{id:1,name:'窗口任务'}],skipped:[]}};await ex('previewBatch()');assert(q('#batchResult').textContent.includes('时区：UTC'));assert(q('#batchResult').textContent.includes('签到窗口 08:00–12:00'));
    q('#sTz').value='Asia/Shanghai';app.window.confirm=()=>{throw new Error('改变时区不得提交旧预览')};await ex('applyBatch()');assert.strictEqual(sent,1);assert(q('#batchApplyBtn').disabled);
  });
  await test('头像冻结账号查询参数无token，缓存自定义兜底及旧图片隔离',async()=>{
    const avatar=q('avatar-fixture');avatar.querySelector=()=>avatar.children.find(n=>!!n.src)||null;app.avatarFixture=avatar;
    ex("selectedAccount='工作 /a';accountEpoch++;state={logged_in:true,tg:{me:{id:7}}};setAvatar(avatarFixture,'','Alpha')");const first=avatar.children[0];assert.strictEqual(first.src,'/avatar?account_id='+encodeURIComponent('工作 /a'));assert(!first.src.includes('token'));
    ex("setAvatar(avatarFixture,'','Alpha')");assert.strictEqual(avatar.children[0],first);
    ex("setAvatar(avatarFixture,'https://example.invalid/avatar','Beta')");const custom=avatar.children[0];custom.onerror();assert.strictEqual(custom.src,'/avatar?account_id='+encodeURIComponent('工作 /a'));custom.onerror();assert.strictEqual(avatar.children[0].textContent,'B');
    ex("setAvatar(avatarFixture,'https://example.invalid/old','Old')");const old=avatar.children[0];ex("selectedAccount='second';accountEpoch++;setAvatar(avatarFixture,'','New')");const current=avatar.children[0];old.onerror();assert.strictEqual(avatar.children[0],current);assert.strictEqual(current.src,'/avatar?account_id=second');
    assert(html.includes('任务消息或规则文本可能包含你自行填写的敏感内容'));assert(html.includes('请妥善保存备份文件，勿公开分享'));
  });
  // 新增 UI / 图标 / 统一签到时间回归：全部使用 mock，不联网获取 JSON 或图片。
  await test('旧签到图标缺省空串，编辑回填，新建关闭取消全部清空',async()=>{
    ex("selectedAccount='default';accountEpoch++;signinGlobalDirty=false;signinGlobal={enabled:false,start:'00:05',end:'00:41'}");
    app.iconTask={id:42,type:'signin',name:'图标任务',target:'@dummy',command:'/sign',win_start:'08:00',win_end:'12:00',enabled:0};
    ex('autos=[iconTask];editAutoItem(42)');assert.strictEqual(q('#sIconUrl').value,'');
    app.iconTask.icon_url='https://dummy.invalid/icon.png';ex('editAutoItem(42)');assert.strictEqual(q('#sIconUrl').value,app.iconTask.icon_url);
    ex("closeAutoForm('signin');openAutoForm('signin')");assert.strictEqual(q('#sIconUrl').value,'');assert.strictEqual(q('#sIconPreview').children[0].textContent,'?');
  });
  await test('新增与编辑签到JSON独立保存安全图标URL并保持禁用状态',async()=>{
    let requests=[];app.api=async(p,o)=>{requests.push([p,o]);return {}};app.loadAutos=async()=>{};
    q('#sName').value='图标签到';q('#sTarget').value='@dummy';q('#sCmd').value='/sign';await ex("saveAuto('signin')");assert.strictEqual(JSON.parse(requests[0][1].body).icon_url,'');
    app.iconTask.icon_url='https://dummy.invalid/picture.png';ex('autos=[iconTask];editAutoItem(42)');q('#sIconUrl').value='https://dummy.invalid/new.png';await ex("saveAuto('signin')");
    assert.strictEqual(requests[1][0],'/api/autos/42');assert.strictEqual(requests[1][1].method,'PUT');assert.strictEqual(requests[1][1]._account.id,'default');assert.strictEqual(JSON.parse(requests[1][1].body).icon_url,'https://dummy.invalid/new.png');assert.strictEqual(JSON.parse(requests[1][1].body).enabled,0);
  });
  await test('脚本数据相对URL凭据控制字符及HTML注入图标全部拒绝保存',async()=>{
    let sent=0;app.api=async()=>{sent++;return {}};
    for(const url of ['javascript:alert(1)','data:image/svg+xml,<svg/>','//dummy.invalid/a','/image.png','https://user:pass@dummy.invalid/a','https://dummy.invalid/\nscript','https://dummy.invalid/\" onerror=alert(1)','https://dummy.invalid/<svg>','https://dummy.invalid/\\evil']){
      ex("cancelAutoEdit('signin');openAutoForm('signin')");q('#sName').value='测试';q('#sTarget').value='@dummy';q('#sCmd').value='/sign';q('#sIconUrl').value=url;await ex("saveAuto('signin')");assert.strictEqual(ex('safeHttpUrl('+JSON.stringify(url)+')'),'');
    }
    assert.strictEqual(sent,0);assert.strictEqual(ex("safeHttpUrl(' HTTP://dummy.invalid/a.png ')"),'HTTP://dummy.invalid/a.png');
  });
  await test('图标img只赋安全返回URL，lazy加载且失败用安全首字母兜底',async()=>{
    const name='<img src=x onerror=bad()>';app.iconName=name;app.fixtureIcon=ex("makeTaskIcon(iconName,'https://dummy.invalid/a%22.png')");
    const box=app.fixtureIcon,img=box.children[0];assert.strictEqual(img.src,'https://dummy.invalid/a%22.png');assert.strictEqual(img.loading,'lazy');assert.strictEqual(img.referrerPolicy,'no-referrer');assert.strictEqual(img.alt,name);assert(!img.onclick);
    img.listeners.error();assert.strictEqual(box.children.length,0);assert.strictEqual(box.textContent,'<');
    const bad=ex("makeTaskIcon('Alpha','javascript:alert(1)')");assert.strictEqual(bad.textContent,'A');assert.strictEqual(bad.children.length,0);
  });
  await test('图标选择必须显式加载，不自动请求JSON，名称URL只通过DOM赋值',async()=>{
    let sent=0;app.api=async()=>{sent++;return {libraries:[{id:'lib1',name:'默认',enabled:true}],icons:[{name:"');bad();//<img>",url:'https://dummy.invalid/a.png',library_id:'lib1'}],errors:[]}};
    ex("cancelAutoEdit('signin');openAutoForm('signin');iconsLoaded=false;iconItems=[];openIconPicker()");assert.strictEqual(sent,0);await ex('loadIcons()');assert.strictEqual(sent,1);
    const choice=q('#iconGrid').children[0];assert.strictEqual(choice.children[1].textContent,"');bad();//<img>");assert(!choice.onclick);
    choice.listeners.click();assert.strictEqual(q('#sIconUrl').value,'https://dummy.invalid/a.png');assert(!q('#iconPicker').classList.contains('show'));assert(q('#af-signin').classList.contains('show'));
  });
  await test('搜索与分页先最多80项，更多累加80，重新搜索重置上限',async()=>{
    app.iconsFixture={libraries:[{id:'l',name:'搜索库',enabled:true}],icons:Array.from({length:191},(_,i)=>({name:'图标 '+i,url:'https://dummy.invalid/'+i+'.png',library_id:'l'})),errors:[]};
    ex('acceptIcons(iconsFixture);openIconPicker()');assert.strictEqual(q('#iconGrid').children.length,80);assert.strictEqual(q('#iconsMore').style.display,'block');ex('moreIcons()');assert.strictEqual(q('#iconGrid').children.length,160);
    q('#iconSearch').value='图标 190';ex('resetIconSearch()');assert.strictEqual(q('#iconGrid').children.length,1);assert.strictEqual(ex('iconsLimit'),80);q('#iconSearch').value='搜索库';ex('resetIconSearch()');assert.strictEqual(q('#iconGrid').children.length,80);
  });
  await test('恶意图标URL禁选并首字母回退，禁用库不出现在选择列表',async()=>{
    app.iconsFixture={libraries:[{id:'off',enabled:false},{id:'on',enabled:true}],icons:[{name:'禁用',url:'https://dummy.invalid/a.png',library_id:'off'},{name:'危险',url:'javascript:alert(1)',library_id:'on'}],errors:[]};ex('acceptIcons(iconsFixture)');q('#iconSearch').value='';ex('resetIconSearch()');
    assert.strictEqual(q('#iconGrid').children.length,1);const b=q('#iconGrid').children[0];assert(b.disabled);assert.strictEqual(b.children[0].textContent,'危');const before=q('#sIconUrl').value;b.listeners.click();assert.strictEqual(q('#sIconUrl').value,before);
  });
  await test('共享库名称URL及错误来源安全文本渲染，不拼进onclick',async()=>{
    const malicious="<img src=x onerror=bad()>');bad();//";app.iconsFixture={libraries:[{id:'bad /id',name:malicious,url:malicious,enabled:true,builtin:false}],icons:[],errors:[{library_id:'bad /id',message:malicious}]};ex('acceptIcons(iconsFixture)');
    const row=q('#iconLibrariesList').children[0];assert(row.children[0].textContent.includes(malicious));assert.strictEqual(row.children[1].textContent,malicious);assert(q('#iconLibraryErrors').textContent.includes(malicious+'：'+malicious));assert.strictEqual(q('#iconPickerErrors').textContent,q('#iconLibraryErrors').textContent);
    for(const b of row.children[2].children)assert(!b.onclick);
  });
  await test('共享图标库新增参数持久化，仅名称URL启用字段且安全库URL校验',async()=>{
    let requests=[];app.api=async(p,o)=>{requests.push([p,o]);return p==='/api/icon-libraries'&&!o?{libraries:[{id:'external',name:'共享',url:'https://dummy.invalid/icons.json',enabled:true,builtin:false}]}:{ok:true,library:{id:'external'}}};
    q('#iconLibraryName').value=' ';q('#iconLibraryUrl').value='https://dummy.invalid/icons.json';await ex('addIconLibrary()');assert.strictEqual(requests[0][0],'/api/icon-libraries');assert.strictEqual(requests[0][1].method,'POST');assert.deepStrictEqual(JSON.parse(requests[0][1].body),{name:'',url:'https://dummy.invalid/icons.json',enabled:true});assert.strictEqual(requests[1][0],'/api/icon-libraries');assert.strictEqual(ex('iconsLoaded'),false);
    q('#iconLibraryUrl').value='javascript:alert(1)';await ex('addIconLibrary()');assert.strictEqual(requests.length,2);
  });
  await test('共享图标库PUT启停和可选名称、DELETE编码ID，内置库不能删',async()=>{
    let requests=[];app.api=async(p,o)=>{requests.push([p,o]);return !o?{libraries:[{id:'external /id',enabled:true,builtin:false},{id:'builtin',name:'TFEL',enabled:true,builtin:true}]}:{ok:true}};
    await ex("updateIconLibrary('external /id',{enabled:false,name:'新名称',unsafe:'ignored'})");assert.strictEqual(requests[0][0],'/api/icon-libraries/external%20%2Fid');assert.strictEqual(requests[0][1].method,'PUT');assert.deepStrictEqual(JSON.parse(requests[0][1].body),{enabled:false,name:'新名称'});
    const before=requests.length;await ex("deleteIconLibrary('builtin')");assert.strictEqual(requests.length,before);const builtin=q('#iconLibrariesList').children[1];assert.strictEqual(builtin.children[2].children.length,2);
    app.window.confirm=()=>true;await ex("deleteIconLibrary('external /id')");assert.strictEqual(requests[before][1].method,'DELETE');assert.strictEqual(requests[before][0],'/api/icon-libraries/external%20%2Fid');assert(!('body' in requests[before][1]));
  });
  await test('图标刷新可按id或全部，返回错误保留来源，失败按钮恢复',async()=>{
    let requests=[];app.api=async(p,o)=>{requests.push([p,o]);return {libraries:[{id:'l',name:'失败来源',enabled:true}],icons:[],errors:[{library_id:'l',message:'<script>timeout</script>'}]}};
    await ex("refreshIcons('l')");assert.strictEqual(requests[0][0],'/api/icons/refresh');assert.strictEqual(requests[0][1].method,'POST');assert.deepStrictEqual(JSON.parse(requests[0][1].body),{id:'l'});assert(q('#iconLibraryErrors').textContent.includes('失败来源：<script>timeout</script>'));
    await ex('refreshIcons()');assert.deepStrictEqual(JSON.parse(requests[1][1].body),{});
    app.api=async()=>{throw new Error('<img>mock failure')};await ex('loadIconLibraries()');assert(q('#iconLibrariesResult').textContent.includes('<img>mock failure'));assert(!q('#iconLibrariesLoad').disabled);await ex('loadIcons()');assert(q('#iconPickerStatus').textContent.includes('<img>mock failure'));assert(!q('#iconsLoad').disabled);
  });
  await test('共享图标接口不带账号头，JSON仅显式按钮加载且无五秒图标轮询',async()=>{
    let requests=[];app.fetch=async(p,o)=>{requests.push([p,o]);return {ok:true,status:200,json:async()=>({})}};
    for(const path of ['/api/icons','/api/icon-libraries','/api/icon-libraries/a','/api/icons/refresh'])await realApi(path);assert(requests.every(r=>!r[1].headers['X-Account-ID']));
    const intervals=js.match(/setInterval\([\s\S]*?\);/g)||[];assert(intervals.every(s=>!/(loadIcons|loadIconLibraries|refreshIcons)/.test(s)));assert(html.includes('https://emby-icon.vercel.app/TFEL-Emby.json'));
  });
  await test('统一签到默认禁用00:05–00:41且字段收起，旧设置缺失兼容',async()=>{
    ex('signinGlobalDirty=false;acceptSigninGlobalSettings({})');assert.strictEqual(ex('signinGlobal.enabled'),false);assert.strictEqual(q('#signinGlobalStart').value,'00:05');assert.strictEqual(q('#signinGlobalEnd').value,'00:41');assert(!q('#signinGlobalEnabled').checked);
    assert(/id="signinGlobalFields" class="hidden"/.test(html));const button=q('global-toggle');app.globalButton=button;ex('toggleSigninGlobalFields(globalButton)');assert.strictEqual(button.getAttribute('aria-expanded'),'true');ex('toggleSigninGlobalFields(globalButton)');assert.strictEqual(button.getAttribute('aria-expanded'),'false');
  });
  await test('统一设置独立POST只保存三个keys，不连带AI或其他设置',async()=>{
    ex("selectedAccount='default';accountEpoch++;signinGlobalDirty=true;signinGlobalBusy=false");q('#signinGlobalEnabled').checked=true;q('#signinGlobalStart').value='00:05';q('#signinGlobalEnd').value='00:41';q('#sAiKey').value='mock-unsaved-key';q('#sAiBase').value='mock-unsaved-base';let requests=[];
    app.api=async(p,o)=>{requests.push([p,o]);return {ok:true}};app.loadAutos=async()=>{};await ex('saveSigninGlobal()');assert.strictEqual(requests.length,1);assert.strictEqual(requests[0][0],'/api/settings');assert.strictEqual(requests[0][1].method,'POST');assert.deepStrictEqual(JSON.parse(requests[0][1].body),{signin_global_enabled:'1',signin_global_start:'00:05',signin_global_end:'00:41'});assert.strictEqual(q('#sAiKey').value,'mock-unsaved-key');assert.strictEqual(q('#sAiBase').value,'mock-unsaved-base');assert.strictEqual(ex('signinGlobal.enabled'),true);assert(!q('#signinGlobalSave').disabled);
  });
  await test('统一设置修改及远端变更均清旧批量预览且提示单任务暂不生效',async()=>{
    ex('batchPreview={old:true};batchSeq=100;markSigninGlobalDirty()');assert.strictEqual(ex('batchPreview'),null);assert(q('#batchApplyBtn').disabled);assert.strictEqual(ex('batchSeq'),101);
    ex("batchPreview={old:true};signinGlobalDirty=false;acceptSigninGlobalSettings({signin_global_enabled:'1',signin_global_start:'01:00',signin_global_end:'01:30'})");assert.strictEqual(ex('batchPreview'),null);assert(ex("describeBatchPatch({win_start:'08:00',win_end:'12:00'})").includes('此修改暂不生效'));
  });
  await test('统一时间启用禁用切换保留原任务窗口，摘要使用有效时间与兼容缺字段',async()=>{
    app.iconTask.win_start='08:00';app.iconTask.win_end='12:00';ex('autos=[iconTask];editAutoItem(42)');assert(q('#sWinStart').disabled&&q('#sWinEnd').disabled);assert.strictEqual(q('#sWinStart').value,'08:00');assert.strictEqual(q('#sWinEnd').value,'12:00');assert(q('#sWindowHint').textContent.includes('原窗口保留但不生效'));
    assert(ex('autoDesc(iconTask)').includes('跟随统一 01:00–01:30'));app.windowTask={...app.iconTask,uses_global_window:true,effective_win_start:'02:00',effective_win_end:'02:10'};assert.strictEqual(ex('autoWindow(windowTask)'),'跟随统一 02:00–02:10');
    ex("signinGlobalDirty=false;acceptSigninGlobalSettings({signin_global_enabled:'0'});syncSigninWindow()");assert(!q('#sWinStart').disabled);assert.strictEqual(q('#sWinStart').value,'08:00');assert(ex('autoWindow(iconTask)').includes('单任务窗口 08:00–12:00'));
  });
  await test('统一时间非法HH:MM不请求，保存失败恢复按钮且不强制run或停任务',async()=>{
    let sent=0;app.api=async()=>{sent++;throw new Error('<script>mock save failure')};q('#signinGlobalStart').value='24:00';await ex('saveSigninGlobal()');assert.strictEqual(sent,0);
    q('#signinGlobalStart').value='00:05';q('#signinGlobalEnd').value='00:41';await ex('saveSigninGlobal()');assert.strictEqual(sent,1);assert(!q('#signinGlobalSave').disabled);assert(q('#signinGlobalResult').textContent.includes('<script>mock save failure'));
    const save=slice('async function saveSigninGlobal(){','function followsGlobalWindow');assert(!/\/run|\/toggle|\/api\/tg|\/api\/ai/.test(save));
  });
  await test('任务卡片默认收起，紧凑头部含选择图标名称状态执行箭头，底部保留管理',async()=>{
    ex("selectedAccount='default';accountEpoch++;openedAutoIds.clear();accounts=[{id:'default',label:'当前账号'}]");app.cardTask={id:7,type:'signin',name:'签到卡片',target:'@dummy',command:'/sign',win_start:'08:00',win_end:'12:00',enabled:1,last_result:{ts:1,ok:true,reply:'完成',detail:'测试详情'}};ex('autos=[cardTask];renderAutoList(\'signin\')');
    const markup=q('#aListSign').innerHTML;for(const token of ['data-auto-action="select"','data-task-icon="7"','签到卡片','已启用','data-auto-action="run"','data-auto-action="expand"','aria-expanded="false"','data-auto-action="toggle"','data-auto-action="edit"','data-auto-action="duplicate"','data-auto-action="delete"'])assert(markup.includes(token));
    assert(!markup.includes('auto-details'));assert(!markup.includes('测试详情'));assert(!markup.includes('命令后'));assert(!markup.includes('onclick='));assert.strictEqual(ex('openedAutoIds.size'),0);
  });
  await test('展开显示原配置当前账号有效窗口上次执行结果，错误回复详情全部转义',async()=>{
    const attack='<img src=x onerror=bad()>';app.cardTask.last_result={ts:1710000000,ok:0,reply:attack,detail:{error:attack}};ex('toggleAutoExpanded(7)');const markup=q('#aListSign').innerHTML;
    for(const token of ['aria-expanded="true"','命令后','归属当前账号：当前账号（default）','有效窗口：单任务窗口 08:00–12:00','上次执行：','失败','实际回复：','详情：','&lt;img'])assert(markup.includes(token));assert(!markup.includes('<img src=x'));ex('toggleAutoExpanded(7)');assert(!q('#aListSign').innerHTML.includes('auto-details'));
  });
  await test('缺上次结果有效窗口与全局标记的旧任务兼容，成功0/1与字符串安全',async()=>{
    app.cardTask.last_result=null;ex('toggleAutoExpanded(7)');assert(q('#aListSign').innerHTML.includes('上次执行结果：暂无记录'));
    for(const ok of [true,1,'1']){app.cardTask.last_result={ts:'<script>time</script>',ok,reply:'done',detail:null};ex("renderAutoList('signin')");assert(q('#aListSign').innerHTML.includes('成功'));assert(q('#aListSign').innerHTML.includes('&lt;script&gt;time'));assert(!q('#aListSign').innerHTML.includes('<script>time'))}
    ex('autos=[{...cardTask,id:"<img>"}];renderAutoList(\'signin\')');assert(!q('#aListSign').innerHTML.includes('auto-card'));
  });
  await test('同账号列表刷新保留打开任务，删除后清理，不跨账号复用taskID',async()=>{
    ex(slice('async function loadAutos(){','function renderAutoList(type){'));app.api=async()=>({autos:[app.cardTask,{id:8,type:'custom',name:'第二任务',target:'@dummy',custom_steps:'[]'}]});ex('openedAutoIds=new Set([7,8,999])');await ex('loadAutos()');assert.deepStrictEqual(copy(ex('[...openedAutoIds]')),[7,8]);assert(q('#aListSign').innerHTML.includes('aria-expanded="true"'));
    app.api=async()=>({autos:[{...app.cardTask,id:9}]});await ex('loadAutos()');assert.strictEqual(ex('openedAutoIds.size'),0);assert(q('#aListSign').innerHTML.includes('aria-expanded="false"'));
    ex('openedAutoIds.add(9)');app.loadAutos=async()=>{};ex("switchAccount('other-account')");assert.strictEqual(ex('openedAutoIds.size'),0);assert.strictEqual(ex('autos.length'),0);ex('autos=[{...cardTask,id:9}];renderAutoList(\'signin\')');assert(!q('#aListSign').innerHTML.includes('auto-details'));
  });
  await test('旧任务DOM事件冻结账号，A到B同taskID不能编辑执行或删除B',async()=>{
    ex("selectedAccount='a';accountEpoch++;autos=[cardTask];renderAutoList('signin')");const oldHandler=q('#aListSign').onclick;app.loadAutos=async()=>{};ex("switchAccount('b');autos=[{...cardTask,name:'账号B任务'}];renderAutoList('signin')");
    let sent=0;app.api=async()=>{sent++;return {}};for(const action of ['edit','run','toggle','delete','duplicate','select','expand'])oldHandler({target:{closest:()=>({dataset:{autoAction:action,autoId:'7'},checked:true})}});
    assert.strictEqual(ex('editAuto'),null);assert.strictEqual(ex('openedAutoIds.size'),0);assert.strictEqual(ex('selectedAutoIds.size'),0);assert.strictEqual(sent,0);
    q('#aListSign').onclick({target:{closest:()=>({dataset:{autoAction:'edit',autoId:'7'}})}});assert.strictEqual(q('#sName').value,'账号B任务');assert.strictEqual(ex('editAutoAccount.id'),'b');
  });
  await test('图标选择换账号清弹窗，旧选择闭包不能改同ID新任务表单',async()=>{
    app.iconsFixture={libraries:[{id:'l',enabled:true}],icons:[{name:'icon',url:'https://dummy.invalid/old.png',library_id:'l'}],errors:[]};ex('acceptIcons(iconsFixture);openIconPicker()');q('#iconSearch').value='';ex('resetIconSearch()');const old=q('#iconGrid').children[0];
    ex("switchAccount('c');autos=[cardTask];editAutoItem(7)");q('#sIconUrl').value='https://dummy.invalid/current.png';old.listeners.click();assert.strictEqual(q('#sIconUrl').value,'https://dummy.invalid/current.png');assert(!q('#iconPicker').classList.contains('show'));assert.strictEqual(ex('iconPickerAccount'),null);
  });
  await test('共享库修改后旧图标选择失效，重新加载再选择',async()=>{
    ex('openIconPicker();acceptIcons(iconsFixture)');const old=q('#iconGrid').children[0];const before=q('#sIconUrl').value;app.api=async()=>({libraries:[{id:'l',enabled:false}],ok:true});await ex("updateIconLibrary('l',{enabled:false})");old.listeners.click();assert.strictEqual(q('#sIconUrl').value,before);assert.strictEqual(ex('iconsLoaded'),false);assert.strictEqual(q('#iconGrid').children.length,0);
  });
  await test('统一设置保存响应晚于切换账号，不覆写新表单状态',async()=>{
    ex("signinGlobalBusy=false;signinGlobalDirty=true;signinGlobal={enabled:false,start:'00:05',end:'00:41'}");q('#signinGlobalEnabled').checked=true;q('#signinGlobalStart').value='02:00';q('#signinGlobalEnd').value='02:30';let resolve;app.api=()=>new Promise(r=>resolve=r);const pending=ex('saveSigninGlobal()');
    ex("switchAccount('d')");q('#signinGlobalStart').value='04:00';const result=q('#signinGlobalResult').textContent;resolve({ok:true});await pending;assert.strictEqual(q('#signinGlobalStart').value,'04:00');assert.strictEqual(q('#signinGlobalResult').textContent,result);assert.strictEqual(ex('signinGlobal.enabled'),false);assert(!q('#signinGlobalSave').disabled);
  });
  await test('页面刷新按账号恢复展开状态，缓存中的恶意或非整数ID过滤，换账号清空',async()=>{
    ex('autos=[cardTask];openedAutoIds.clear();toggleAutoExpanded(7)');const saved=JSON.parse(sessionStore.get('tgp_open_autos'));assert.strictEqual(saved.account,ex('selectedAccount'));assert.deepStrictEqual(saved.ids,[7]);
    const stateCode=slice('let openedAutoIds=new Set();','let autos=[], editAuto=null, editAutoAccount=null;');
    for(const [account,expected] of [[saved.account,[7]],['different',[]]]){const fresh=vm.createContext({selectedAccount:account,sessionStorage:{getItem:()=>JSON.stringify({...saved,ids:[7,'<img>',null,1.5]})}});vm.runInContext(stateCode,fresh);assert.deepStrictEqual(copy(vm.runInContext('[...openedAutoIds]',fresh)),expected)}
    ex("switchAccount('refresh-other')");assert.deepStrictEqual(JSON.parse(sessionStore.get('tgp_open_autos')).ids,[]);
  });
  await test('同账号旧settings读取晚于统一时间保存，不能回写旧全局窗口',async()=>{
    ex(slice('async function loadSettings(){','async function saveSettings(){'));app.loadTz=()=>{};app.loadSessions=()=>{};app.loadAutos=async()=>{};ex("signinGlobalDirty=false;signinGlobalBusy=false;signinGlobal={enabled:false,start:'00:05',end:'00:41'}");
    let oldGet;app.api=(p,o)=>o?.method==='POST'?Promise.resolve({ok:true}):new Promise(r=>oldGet=r);const pending=ex('loadSettings()');q('#signinGlobalEnabled').checked=true;q('#signinGlobalStart').value='05:00';q('#signinGlobalEnd').value='05:30';await ex('saveSigninGlobal()');
    oldGet({signin_global_enabled:'0',signin_global_start:'00:05',signin_global_end:'00:41'});await pending;assert.strictEqual(ex('signinGlobal.enabled'),true);assert.strictEqual(q('#signinGlobalStart').value,'05:00');assert.strictEqual(q('#signinGlobalEnd').value,'05:30');
  });
  await test('AI助手默认折叠且生成预览不保存、恶意文本只作为文字',async()=>{
    assert(/class="card fold" id="aiAssistantCard"/.test(html));
    assert(html.includes('onclick="foldToggle(this)"><span class="lbl">AI 助手'));
    ex("selectedAccount='default';accountEpoch++;resetAIAssistant();autos=[]");
    q('#aiDraftText').value='给 @dummy 发 /start';
    app.currentAIConfig=()=>({ai_base:'https://dummy.invalid',ai_key:'',ai_model:'fixture'});
    app.generated={type:'signin',name:'<img src=x onerror=bad()>',target:'@dummy',command:'/start',enabled:0,command_delay:2,button_delay:3,retries:0};
    let requests=[];app.api=async(p,o)=>{requests.push([p,JSON.parse(o.body),o._account]);return {ok:true,draft:app.generated,summary:app.generated.name,warnings:['核对后启用'],elapsed_ms:25}};
    await ex('generateAIDraft()');assert.strictEqual(requests.length,1);assert.strictEqual(requests[0][0],'/api/ai/task-draft');assert.strictEqual(requests[0][2].id,'default');
    assert(q('#aiDraftResult').textContent.includes('<img'));assert(q('#aiDraftResult').textContent.includes('命令后等待（秒）'));assert(q('#aiDraftResult').textContent.includes('核对后启用'));assert(!q('#aiDraftBtn').disabled);
  });
  await test('AI填入表单不是编辑已有任务，保存仍默认停用且保留零次重试',async()=>{
    let sent=[];app.api=async(p,o)=>{sent.push([p,JSON.parse(o.body)]);return {ok:true}};
    ex('fillAIDraft()');assert.strictEqual(sent.length,0);assert.strictEqual(ex('editAuto'),null);
    assert.strictEqual(q('#sName').value,app.generated.name);assert.strictEqual(q('#sRetries').value,'0');
    assert(q('#af-signin').classList.contains('show'));await ex("saveAuto('signin')");
    assert.strictEqual(sent[0][0],'/api/autos');assert.strictEqual(sent[0][1].enabled,0);assert.strictEqual(sent[0][1].retries,0);
  });
  await test('AI直接保存二次确认，拒绝不提交、允许仅保存配置',async()=>{
    ex('aiDraft={account:accountContext(),draft:generated};aiDraftBusy=false');
    let sent=[];app.api=async(p,o)=>{sent.push([p,JSON.parse(o.body)]);return {ok:true}};
    app.window.confirm=()=>false;await ex('saveAIDraft()');assert.strictEqual(sent.length,0);
    app.window.confirm=()=>true;await ex('saveAIDraft()');assert.strictEqual(sent.length,1);assert.strictEqual(sent[0][1].enabled,0);assert.strictEqual(sent[0][1].signin_captcha,0);
  });
  await test('AI草稿请求防重复，描述改变后丢弃旧结果并恢复按钮',async()=>{
    ex('resetAIAssistant()');q('#aiDraftText').value='old';let resolve,hits=0;
    app.api=()=>{hits++;return new Promise(r=>resolve=r)};const pending=ex('generateAIDraft()');await ex('generateAIDraft()');assert.strictEqual(hits,1);
    q('#aiDraftText').value='new';ex('invalidateAIDraft()');resolve({ok:true,draft:app.generated});await pending;
    assert.strictEqual(ex('aiDraft'),null);assert(!q('#aiDraftBtn').disabled);
    app.api=async()=>({ok:false,error:'接口不可用'});await ex('generateAIDraft()');assert(q('#aiDraftResult').textContent.includes('接口不可用'));
  });
  await test('AI排障读取当前任务并展示应用按钮，不保存任务',async()=>{
    ex("resetAIAssistant();autos=[{...generated,id:91,name:'AI排障任务',enabled:1}]");
    app.api=async(p,o)=>{assert.strictEqual(p,'/api/ai/diagnose');assert.strictEqual(JSON.parse(o.body).id,91);return {ok:true,analysis:'<img src=x>',suggestions:[{field:'button_delay',value:4,reason:'等待回复'},{field:'enabled',value:1,reason:'越权字段'}],elapsed_ms:30}};
    await ex('diagnoseAuto(91)');assert.strictEqual(ex('aiDiagnoses.get(91).suggestions.length'),1);
    assert(q('#aiDiagResult91').textContent.includes('<img'));assert.strictEqual(q('#aiDiagActions91').children.length,1);
    assert.strictEqual(q('#aiDiagActions91').children[0].children[1].textContent,'应用此建议');
  });
  await test('AI建议先核对最新配置，仅修改允许字段且取消不提交',async()=>{
    let puts=[],gets=0;app.api=async(p,o)=>{if(o?.method==='PUT'){puts.push(JSON.parse(o.body));return {ok:true}}gets++;return {autos:copy(ex('autos'))}};
    app.window.confirm=()=>false;await ex('applyAISuggestion(91,0)');assert.strictEqual(puts.length,0);assert.strictEqual(gets,0);
    app.window.confirm=()=>true;const before=copy(ex('taskConfig(autos[0])'));await ex('applyAISuggestion(91,0)');assert.strictEqual(gets,1);assert.strictEqual(puts.length,1);
    assert.deepStrictEqual(puts[0],{...before,button_delay:4});
  });
  await test('AI建议不能覆盖分析后已修改的配置或另一个账号',async()=>{
    ex("aiDiagnoses.set(91,{account:accountContext(),fingerprint:taskFingerprint(autos[0]),applied:new Set(),suggestions:[{field:'button_delay',value:4}]})");
    let writes=0;app.api=async(p,o)=>{if(o?.method==='PUT')writes++;return {autos:[{...copy(ex('autos[0]')),command:'/changed'}]}};
    await ex('applyAISuggestion(91,0)');assert.strictEqual(writes,0);
    ex("selectedAccount='switched';accountEpoch++");await ex('applyAISuggestion(91,0)');assert.strictEqual(writes,0);ex('resetAIAssistant()');assert.strictEqual(ex('aiDiagnoses.size'),0);
  });
  await test('AI草稿切换账号时丢弃迟到结果',async()=>{
    q('#aiDraftText').value='task';let resolve;app.api=()=>new Promise(r=>resolve=r);const pending=ex('generateAIDraft()');
    ex("selectedAccount='another';accountEpoch++;resetAIAssistant()");resolve({ok:true,draft:app.generated});await pending;assert.strictEqual(ex('aiDraft'),null);assert(!q('#aiDraftBtn').disabled);
  });
  await test('最近执行待确认保留原因实际回复与当日不重发说明',async()=>{
    for(const result of [{ok:0,detail:{status:'unconfirmed',reason:'等待回复超时'}},{ok:null,detail:{status:'unconfirmed',error:'缺少最终证据'}},{ok:true,status:'unconfirmed',detail:null}]){
      app.pendingTask={type:'signin',last_result:{ts:1,reply:'处理中',...result}};const text=ex('autoLastResult(pendingTask)');
      assert(text.includes(' · 待确认'));assert(!text.includes(' · 失败')&&!text.includes(' · 成功'));assert(text.includes('实际回复：处理中'));assert(text.includes('待确认原因：'));assert(text.includes('本日不自动重发'));
      assert(text.includes(result.detail?.reason||result.detail?.error||'等待最终回复超时，结果仍未知'));
    }
  });
  await test('待确认任务卡片独立保留启用停用及折叠操作并转义原因',async()=>{
    for(const enabled of [0,1])for(const topStatus of [false,true]){
      app.pendingTask={id:501,type:'signin',name:'待确认任务',enabled,target:'@dummy',command:'/sign',status:topStatus?'unconfirmed':undefined,last_result:{ts:1,ok:0,reply:'<img src=x>',detail:{status:topStatus?undefined:'unconfirmed',reason:'<script>reason</script>'}}};
      ex("autos=[pendingTask];openedAutoIds.clear();renderAutoList('signin')");let markup=q('#aListSign').innerHTML;
      assert(markup.includes('class="tag pending" role="status">待确认'));assert(markup.includes(enabled?'已启用':'已停用'));assert(!markup.includes('auto-details'));assert(markup.includes('aria-expanded="false"'));
      assert(markup.includes('data-auto-action="toggle"'));assert.strictEqual(ex('autos[0].enabled'),enabled);
      ex('toggleAutoExpanded(501)');markup=q('#aListSign').innerHTML;assert(markup.includes('待确认原因'));assert(markup.includes('&lt;script&gt;reason'));assert(!markup.includes('<script>reason')&&!markup.includes('<img src=x>'));
    }
  });
  await test('AI 填入草稿结果等待默认零与边界并完整保存为停用',async()=>{
    for(const fields of [{},{result_delay:null,result_timeout:null},{result_delay:0,result_timeout:1},{result_delay:120,result_timeout:300},{result_delay:0.1,result_timeout:30}]){
      app.resultDraft={type:'signin',name:'等待结果草稿',target:'@dummy',command:'/sign',retries:0,...fields};ex('resetAIAssistant();aiDraft={account:accountContext(),draft:resultDraft}');let sent=[];
      app.api=async(p,o)=>{sent.push([p,o]);return {}};ex('fillAIDraft()');assert.strictEqual(sent.length,0);
      assert.strictEqual(q('#sResultDelay').value,String(fields.result_delay??0));assert.strictEqual(q('#sResultTimeout').value,String(fields.result_timeout??30));
      await ex("saveAuto('signin')");assert.strictEqual(sent.length,1);const [url,options]=sent[0],body=JSON.parse(options.body);
      assert.strictEqual(url,'/api/autos');assert.strictEqual(body.result_delay,fields.result_delay??0);assert.strictEqual(body.result_timeout,fields.result_timeout??30);assert.strictEqual(body.retries,0);assert.strictEqual(body.enabled,0);
    }
  });
  await test('AI 配置投影保留结果等待排除运行状态并参与变更指纹',async()=>{
    app.resultTask={id:601,type:'signin',name:'AI 参数保留',target:'@dummy',command:'/sign',enabled:0,command_delay:0,button_delay:2,result_delay:0,result_timeout:300,retries:0,icon_url:'https://dummy.invalid/icon.png',last_result:{ok:0,detail:{status:'unconfirmed'}},status:'unconfirmed'};
    const projected=copy(ex('taskConfig(resultTask)'));assert.strictEqual(projected.result_delay,0);assert.strictEqual(projected.result_timeout,300);assert.strictEqual(projected.retries,0);assert(!('id' in projected)&&!('last_result' in projected)&&!('status' in projected));
    const before=ex('taskFingerprint(resultTask)');ex('resultTask.result_timeout=30');assert.notStrictEqual(ex('taskFingerprint(resultTask)'),before);ex('resultTask.result_timeout=300;resultTask.result_delay=0.1');assert.notStrictEqual(ex('taskFingerprint(resultTask)'),before);ex('resultTask.result_delay=0');
  });
  await test('AI 结果等待建议接受合法数值拒绝越界步进和错误类型',async()=>{
    for(const [field,valid,invalid,label] of [['result_delay',[0,0.1,120],[-0.1,120.1,1.05,'0',null,Infinity,NaN],'结果判断前延迟（秒）'],['result_timeout',[1,30,300],[0,301,1.1,'30',null,Infinity,NaN],'等待最终回复上限（秒）']]){
      for(const value of valid){app.suggestion={field,value};assert(ex("aiSuggestionValid(suggestion,'signin')"));assert(!ex("aiSuggestionValid(suggestion,'custom')"));assert(!ex("aiSuggestionValid(suggestion,'keepalive')"));}
      for(const value of invalid){app.suggestion={field,value};assert(!ex("aiSuggestionValid(suggestion,'signin')"));}
      app.field=field;assert.strictEqual(ex('AI_LABELS[field]'),label);assert.strictEqual(ex('AI_DIAG_LABELS[field]'),label);
    }
  });
  await test('AI 应用其他建议 PUT 仍保留结果等待完整配置和零重试',async()=>{
    ex("resetAIAssistant();autos=[resultTask];aiDiagnoses.set(601,{account:accountContext(),fingerprint:taskFingerprint(resultTask),applied:new Set(),suggestions:[{field:'button_delay',value:4}]})");
    const before=copy(ex('taskConfig(resultTask)'));let requests=[];app.window.confirm=()=>true;
    app.api=async(p,o)=>{requests.push([p,o]);return o?.method==='PUT'?{}:{autos:[copy(app.resultTask)]}};
    await ex('applyAISuggestion(601,0)');assert.strictEqual(requests.length,2);assert.strictEqual(requests[1][0],'/api/autos/601');assert.strictEqual(requests[1][1].method,'PUT');assert.deepStrictEqual(JSON.parse(requests[1][1].body),{...before,button_delay:4});
  });
  await test('AI 结果等待单项建议确认后保存其余参数完全不变',async()=>{
    for(const [field,value] of [['result_delay',0.1],['result_timeout',1]]){
      app.suggestion={field,value};ex('resetAIAssistant();autos=[resultTask];aiDiagnoses.set(601,{account:accountContext(),fingerprint:taskFingerprint(resultTask),applied:new Set(),suggestions:[suggestion]})');let puts=[];
      app.api=async(p,o)=>{if(o?.method==='PUT')puts.push(JSON.parse(o.body));return {autos:[copy(app.resultTask)]}};const before=copy(ex('taskConfig(resultTask)'));
      app.window.confirm=()=>false;await ex('applyAISuggestion(601,0)');assert.strictEqual(puts.length,0);
      app.window.confirm=()=>true;await ex('applyAISuggestion(601,0)');assert.deepStrictEqual(puts,[{...before,[field]:value}]);
    }
  });
  await test('AI 直接保存草稿白名单保留结果等待且不携带运行状态',async()=>{
    ex('resetAIAssistant();aiDraft={account:accountContext(),draft:resultTask}');let sent=[];app.window.confirm=()=>true;app.api=async(p,o)=>{sent.push([p,JSON.parse(o.body)]);return {}};
    await ex('saveAIDraft()');assert.strictEqual(sent.length,1);assert.strictEqual(sent[0][0],'/api/autos');assert.strictEqual(sent[0][1].result_delay,0);assert.strictEqual(sent[0][1].result_timeout,300);assert.strictEqual(sent[0][1].enabled,0);assert(!('last_result' in sent[0][1])&&!('status' in sent[0][1]));
  });
  await test('并发控件默认二且说明清楚每批与整轮重试',async()=>{
    const control=html.match(/<select id="sTaskConcurrency">([\s\S]*?)<\/select>/);assert(control);assert(control[1].includes('<option value="2" selected>2</option>'));
    for(let i=1;i<=5;i++)assert(control[1].includes('value="'+i+'"'));assert(html.includes('按任务列表顺序，每批两个；整轮结束再排失败重试'));
  });
  await test('设置并发按账号载入保存且迟到响应不覆盖新账号',async()=>{
    app.loadTz=app.loadSessions=()=>{};ex("selectedAccount='settings-a';accountEpoch++");let resolves=[];app.api=()=>new Promise(r=>resolves.push(r));
    const old=ex('loadSettings()');ex("selectedAccount='settings-b';accountEpoch++");const fresh=ex('loadSettings()');
    resolves[1]({task_concurrency:'5',ai_key_present:false});await fresh;assert.strictEqual(q('#sTaskConcurrency').value,'5');
    resolves[0]({task_concurrency:'1',ai_key_present:true});await old;assert.strictEqual(q('#sTaskConcurrency').value,'5');
    q('#sTaskConcurrency').value='4';q('#sAiKey').value='';let posted;app.api=async(p,o)=>{if(o?.method==='POST'){posted=[p,o];return {ok:true}}return {task_concurrency:'4',ai_key_present:false}};
    await ex('saveSettings()');assert.strictEqual(posted[0],'/api/settings');assert.strictEqual(posted[1]._account.id,'settings-b');assert.strictEqual(JSON.parse(posted[1].body).task_concurrency,'4');assert.strictEqual(JSON.parse(posted[1].body).ai_key,'');
  });
  await test('AI Key 永不回填，保存后清空并只更新存在提示',async()=>{
    ex("selectedAccount='default';accountEpoch++");app.loadTz=app.loadSessions=()=>{};
    app.api=async()=>({task_concurrency:'2',ai_key:'<img src=x onerror=bad()>',ai_key_present:true});await ex('loadSettings()');
    assert.strictEqual(q('#sAiKey').value,'');assert.strictEqual(q('#sAiKey').placeholder,'已保存，留空不修改');assert(!q('#sAiKey').innerHTML.includes('img'));
    q('#sAiKey').value='mock-new-key';let body;app.api=async(p,o)=>{if(o?.method==='POST'){body=JSON.parse(o.body);return {ok:true}}return {task_concurrency:'2',ai_key:'malicious-returned-key',ai_key_present:true}};
    await ex('saveSettings()');assert.strictEqual(body.ai_key,'mock-new-key');assert.strictEqual(q('#sAiKey').value,'');assert.strictEqual(q('#sAiKey').placeholder,'已保存，留空不修改');
    assert(![...store.values()].join('|').includes('mock-new-key'));
  });
  await test('运行与排队任务显示停止取消按钮并正确启用停止全部',async()=>{
    app.runTask={id:701,type:'signin',name:'运行任务',target:'@dummy',command:'/sign',enabled:1,running:true,queued:false};app.queueTask={id:702,type:'signin',name:'排队任务',target:'@dummy',command:'/sign',enabled:1,running:false,queued:true};
    ex('autos=[runTask,queueTask];autoStopBusy.clear();autoStopAllBusy=false;renderAutoList("signin");syncStopAllButton()');const markup=q('#aListSign').innerHTML;
    assert(markup.includes('data-auto-action="stop"'));assert(markup.includes('>停止</button>'));assert(markup.includes('>取消排队</button>'));assert(markup.includes('>运行中</span>'));assert(markup.includes('>排队中</span>'));assert(!q('#autoStopAll').disabled);
    ex('autos=[{...runTask,running:false,queued:false}];syncStopAllButton()');assert(q('#autoStopAll').disabled);
  });
  await test('单项停止二次确认防重复并携带冻结账号',async()=>{
    ex("selectedAccount='stop-a';accountEpoch++;autos=[runTask];autoStopBusy.clear();autoStopAllBusy=false");let requests=[],done,loads=0;app.loadAutos=async()=>{loads++};
    app.window.confirm=message=>{assert(message.includes('中止当前正在执行的步骤'));return false};app.api=async(p,o)=>{requests.push([p,o]);return {ok:true}};await ex('stopAuto(701)');assert.strictEqual(requests.length,0);
    app.window.confirm=()=>true;app.api=(p,o)=>{requests.push([p,o]);return new Promise(r=>done=r)};const pending=ex('stopAuto(701)');await Promise.resolve();const duplicate=ex('stopAuto(701)');assert.strictEqual(requests.length,1);assert.strictEqual(requests[0][0],'/api/autos/701/stop');assert.strictEqual(requests[0][1]._account.id,'stop-a');done({ok:true,status:'stopped'});await pending;await duplicate;assert.strictEqual(loads,1);assert.strictEqual(ex('autoStopBusy.size'),0);
  });
  await test('旧任务DOM不能停止新账号同ID任务且停止全部需确认',async()=>{
    ex("selectedAccount='old-stop';accountEpoch++;autos=[runTask];renderAutoList('signin')");const oldHandler=q('#aListSign').onclick;let requests=[];app.api=async(p,o)=>{requests.push([p,o]);return {ok:true,stopped:[701,702]}};app.loadAutos=async()=>{};app.window.confirm=()=>true;
    ex("selectedAccount='new-stop';accountEpoch++;autos=[{...runTask,name:'新账号同ID'}]");oldHandler({target:{closest:()=>({dataset:{autoAction:'stop',autoId:'701'}})}});assert.strictEqual(requests.length,0);
    ex('autos=[runTask,queueTask];autoStopAllBusy=false');app.window.confirm=()=>false;await ex('stopAllAutos()');assert.strictEqual(requests.length,0);
    let done;app.window.confirm=message=>{assert(message.includes('中止当前步骤'));return true};app.api=(p,o)=>{requests.push([p,o]);return new Promise(r=>done=r)};const stopping=ex('stopAllAutos()');await Promise.resolve();const duplicate=ex('stopAllAutos()');assert.strictEqual(requests.length,1);assert.strictEqual(requests[0][0],'/api/autos/stop-all');assert.strictEqual(requests[0][1]._account.id,'new-stop');done({ok:true,stopped:[701,702]});await stopping;await duplicate;assert.strictEqual(ex('autoStopAllBusy'),false);
  });
  const realAnomalyLoader=app.loadAnomalies;
  await test('异常分类走独立接口并安全渲染状态次数时间回复',async()=>{
    ex("selectedAccount='anomaly-a';accountEpoch++;logKind='anomaly';anomalySeq++;anomalies=[]");app.loadLogs=realLogLoader;let paths=[];app.api=async p=>{paths.push(p);return {count:4,items:[
      {id:'x<1',name:'<img src=x onerror=bad()>',type:'signin',status:'failed',retry_count:1,retry_limit:3,last_attempt:'昨天',next_run_at:'明天',running:false,queued:false,last_result:{reply:'<script>bad()</script>'}},
      {id:2,name:'排队异常',type:'custom',status:'interrupted',retry_count:2,retry_limit:2,running:false,queued:true,last_result:null},
      {id:3,name:'待确认异常',type:'keepalive',status:'unconfirmed',retry_count:0,retry_limit:1,running:false,queued:false,last_result:null},
      {id:4,name:'停止异常',type:'custom',status:'stopped',retry_count:0,retry_limit:0,running:false,queued:false,last_result:null}
    ]}};await ex('loadLogs()');assert.deepStrictEqual(paths,['/api/anomalies']);const markup=q('#logList').innerHTML;
    assert(markup.includes('&lt;img src=x onerror=bad()&gt;'));assert(markup.includes('&lt;script&gt;bad()&lt;/script&gt;'));assert(!markup.includes('<img src=x')&&!markup.includes('<script>bad()'));
    assert(markup.includes('失败'));assert(markup.includes('中断'));assert(markup.includes('待确认'));assert(markup.includes('已停止'));assert(markup.includes('重试 1 / 3'));assert(markup.includes('上次 昨天'));assert(markup.includes('最后回复：'));assert(markup.includes('确认成功'));assert(markup.includes('disabled'));
  });
  await test('异常四项动作发送精确请求体并刷新异常与任务',async()=>{
    let requests=[],refreshA=0,refreshT=0;app.api=async(p,o)=>{requests.push([p,o]);return {ok:true}};app.loadAnomalies=async()=>{refreshA++};app.loadAutos=async()=>{refreshT++};app.window.confirm=()=>true;
    for(const action of ['confirm_success','mark_failed','retry_once','disable']){
      ex("anomalies=[{id:801,name:'待处理',type:'signin',status:'unconfirmed',running:false,queued:false}];anomalyBusyIds.clear()");await ex(`resolveAnomaly('801','${action}')`);
      const [path,options]=requests.at(-1);assert.strictEqual(path,'/api/anomalies/801/resolve');assert.strictEqual(options.method,'POST');assert.deepStrictEqual(JSON.parse(options.body),{action});assert.strictEqual(options._account.id,'anomaly-a');
    }
    assert.strictEqual(requests.length,4);assert.strictEqual(refreshA,4);assert.strictEqual(refreshT,4);
  });
  await test('异常动作取消、运行排队禁用且非签到不可确认成功',async()=>{
    let sent=0;app.api=async()=>{sent++;return {}};app.loadAnomalies=async()=>{};app.loadAutos=async()=>{};app.window.confirm=()=>false;
    ex("anomalies=[{id:1,name:'取消',type:'signin',running:false,queued:false}]");await ex("resolveAnomaly('1','mark_failed')");assert.strictEqual(sent,0);
    app.window.confirm=()=>true;for(const state of [{running:true,queued:false},{running:false,queued:true}]){app.stateFix=state;ex("anomalies=[{id:2,name:'忙碌',type:'signin',...stateFix}]");await ex("resolveAnomaly('2','retry_once')")};
    ex("anomalies=[{id:3,name:'自定义',type:'custom',running:false,queued:false}]");await ex("resolveAnomaly('3','confirm_success')");assert.strictEqual(sent,0);
  });
  await test('异常旧响应在切账号清空后不得覆盖新账号',async()=>{
    app.loadAnomalies=realAnomalyLoader;ex("selectedAccount='stale-a';accountEpoch++;logKind='anomaly';anomalies=[{id:9,name:'旧缓存'}]");let done;app.api=()=>new Promise(r=>done=r);const pending=ex('loadAnomalies()');
    ex("selectedAccount='stale-b';accountEpoch++;anomalySeq++;anomalies=[]");done({count:1,items:[{id:10,name:'迟到异常',type:'signin',status:'failed'}]});await pending;assert.strictEqual(ex('anomalies.length'),0);assert(!q('#logList').innerHTML.includes('迟到异常'));
  });
  await test('隐藏时暂停五秒轮询，回前台立即刷新当前分类',async()=>{
    let tick,states=0,logs=0;app.setInterval=fn=>{tick=fn;return 99};app.clearInterval=()=>{};app.api=async()=>({});
    app.loadState=()=>{states++};app.loadLogs=()=>{logs++};for(const name of ['loadAccounts','loadSettings','loadRules','loadAutos','loadRuleGroups','renderAccounts','renderEmojis','pickAction','pickGrabAct','applyTheme'])app[name]=()=>{};
    ex("activeTab='logs';bootstrapTimer=null");await ex('bootstrap()');states=logs=0;
    ex("document.visibilityState='hidden'");tick();assert.strictEqual(states,0);assert.strictEqual(logs,0);ex('document.listeners.visibilitychange()');assert.strictEqual(states,0);assert.strictEqual(logs,0);
    ex("document.visibilityState='visible'");assert.strictEqual(ex('pageIsHidden()'),false);ex('refreshVisibleTab()');assert.strictEqual(states,1);assert.strictEqual(logs,1);assert(js.includes("addEventListener('visibilitychange',()=>{if(!pageIsHidden())refreshVisibleTab()})"));tick();assert.strictEqual(states,2);assert.strictEqual(logs,2);
  });
  console.log(`前端回归 ${count} 项全部通过`);
})().catch(e=>{console.error(e);process.exitCode=1});
