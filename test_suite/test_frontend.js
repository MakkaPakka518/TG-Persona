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
  element('#sAiOn').checked=ai;element('#sOcrOn').checked=ocr;
}
function selectImage(file={type:'image/png',size:128},image=sampleImage){
  element('#captchaTestFile').files=file?[file]:[];run('previewCaptchaFile()');
  if(file&&image)readers.at(-1).complete(image);
}
function element(id){
  if(!elements.has(id)){
    const classes=new Set();
    const el={innerHTML:'',textContent:'',children:[],style:{},dataset:{},disabled:false,src:'',listeners:{},attributes:{},querySelector(){return null},querySelectorAll(){return []},getAttribute(k){return this.attributes[k]??null},setAttribute(k,v){this.attributes[k]=String(v)},click(){this.clicked=true},focus(){this.focused=true},remove(){this.removed=true},classList:{add(...v){v.forEach(k=>classes.add(k))},remove(...v){v.forEach(k=>classes.delete(k))},contains(k){return classes.has(k)},toggle(k,force){const add=force===undefined?!classes.has(k):force;add?classes.add(k):classes.delete(k);return add}},scrollIntoView(){},addEventListener(t,fn){this.listeners[t]=fn},contains(){return true},removeAttribute(k){delete this[k]},appendChild(node){this.children.push(node);return node},replaceChildren(...nodes){this.children=nodes;this.textContent=''}};
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
run(js.split('\n').find(line=>line.startsWith('const DEFAULT_SIGNIN_IMAGE_PROMPT=')));
run(js.split('\n').find(line=>line.startsWith('const DEFAULT_AI_FLOCK_PROMPT=')));
run(js.split('\n').find(line=>line.startsWith('const DEFAULT_REGISTRATION_AI_FLOCK_PROMPT=')));
run(slice('// ---------- AI API 池 ----------','// ---------- 状态 ----------'));
run(slice('// ---------- 账号请求范围 ----------','async function api('));
run(slice('// ---------- 安全图标与共享图标库 ----------','let autos=[], editAuto=null, editAutoAccount=null;'));
run(slice('// ---------- 可视化步骤编辑器 ----------','const autoGroupSel='));
run(slice('let allGroups=[];','async function loadDialogs'));
run(slice('function signinRecognitionMode(a){','function dupAuto'));
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
  await test('定时抢注表单提供秒级时间和完整单次流程字段',async()=>{
    const form=html.slice(html.indexOf('id="af-scheduled_register"'),html.indexOf('id="af-grabcodes"'));
    for(const id of ['trName','trDate','trTime','trSecond','trTarget','trCommand','trButton','trHistoryFirst','trClosedRetry','trClosedKeywords','trClosedInterval','trTrigger','trReply','trSuccess','trTimeout'])assert(form.includes('id="'+id+'"'));
    const date=form.match(/<input\b[^>]*id="trDate"[^>]*>/)[0];assert(date.includes('type="text"'));assert(date.includes('maxlength="10"'));
    const time=form.match(/<input\b[^>]*id="trTime"[^>]*>/)[0];assert(time.includes('type="text"'));assert(time.includes('readonly'));assert(time.includes("openTimePicker('trTime')"));
    const second=form.match(/<input\b[^>]*id="trSecond"[^>]*>/)[0];for(const attr of ['type="number"','min="0"','max="59"','step="1"'])assert(second.includes(attr));assert(form.includes('执行一次后不会自动重试'));
  });
  await test('定时抢注保存精确提交目标、指令、按钮、触发、回复和成功关键词',async()=>{
    run("cancelAutoEdit('scheduled_register')");
    const values={trName:'限时注册',trDate:'20261030',trTime:'09:00',trSecond:'1',trTarget:'@dummy_bot',trCommand:'/start',trButton:'开始注册',trTrigger:'请输入注册码',trReply:'TEST-CODE',trSuccess:'注册成功',trTimeout:'45'};
    for(const [id,value] of Object.entries(values))element('#'+id).value=value;
    element('#trHistoryFirst').checked=true;
    element('#trClosedRetry').checked=true;element('#trClosedKeywords').value='注册已关闭';element('#trClosedInterval').value='7';
    let body;context.api=async(_path,options)=>{body=JSON.parse(options.body);return {}};
    await run("saveAuto('scheduled_register')");
    assert.deepStrictEqual(JSON.parse(JSON.stringify(body)),{type:'scheduled_register',name:'限时注册',scheduled_at:'2026-10-30T09:00:01',target:'@dummy_bot',command:'/start',click_btn:1,btn_text:'开始注册',history_first:1,fail_kw:'注册已关闭',poll_interval:7,keywords:'请输入注册码',reply_text:'TEST-CODE',success_kw:'注册成功',result_timeout:45,retries:0});
  });
  await test('签到表单包含两段中文秒数标签、默认值及范围步进',async()=>{
    const form=html.slice(html.indexOf('id="af-signin"'),html.indexOf('id="af-grabcodes"'));
    for(const [id,label,value] of [['sCommandDelay','发送命令后延迟（秒）','1.6'],['sButtonDelay','点击按钮后延迟（秒）','2']]){
      assert(form.includes('<label for="'+id+'">'+label+'</label>'));
      const input=form.match(new RegExp('<input\\b[^>]*id="'+id+'"[^>]*>'))[0];
      for(const attr of ['type="number"','value="'+value+'"','min="0"','max="120"','step="0.1"','required'])assert(input.includes(attr));
    }
    assert(form.includes('发送命令 → 等待命令后延迟 → 点击按钮 → 等待按钮后延迟 → 后续图片处理'));
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
  await test('签到验证码可多选 AI API 并在编辑时完整回填',async()=>{
    const first='0123456789abcdef',second='fedcba9876543210';
    context.providerFixture=[{id:first,note:'主线路',enabled:true},{id:second,note:'备用线路',enabled:true},{id:'aaaaaaaaaaaaaaaa',note:'停用线路',enabled:false}];
    run('aiProviders=providerFixture;renderSigninAIProviders()');prepareSignin();element('#sRecognitionMode').value='captcha';
    assert(!element('#sCaptchaProviders').innerHTML.includes('停用线路'));
    for(const id of [first,second])element('#sCaptchaProviders').listeners.click({target:{closest:()=>({dataset:{signinAi:id}})}});
    let body;context.api=async(_p,o)=>{body=JSON.parse(o.body);return {}};await run("saveAuto('signin')");
    assert.strictEqual(body.ai_provider_ids,first+','+second);
    editSignin({ai_provider_ids:second});assert.strictEqual(run("[...signinAIProviderSel].join(',')"),second);
    assert(element('#sCaptchaProviders').innerHTML.includes('备用线路'));
  });
  await test('签到图片处理三选一，图片模式保存自定义提示词并兼容旧验证码任务',async()=>{
    const form=html.slice(html.indexOf('id="af-signin"'),html.indexOf('id="af-grabcodes"'));
    for(const mode of ['none','captcha','image'])assert(form.includes('<option value="'+mode+'"'));
    prepareSignin();assert.strictEqual(element('#sImagePrompt').value,run('DEFAULT_SIGNIN_IMAGE_PROMPT'));
    element('#sRecognitionMode').value='image';element('#sImagePrompt').value='只输出物体名称';let body;
    context.api=async(_p,o)=>{body=JSON.parse(o.body);return {}};await run("saveAuto('signin')");
    assert.strictEqual(body.signin_recognition_mode,'image');assert.strictEqual(body.signin_captcha,0);assert.strictEqual(body.signin_image_prompt,'只输出物体名称');assert.strictEqual(body.signin_image_action,'reply');
    editSignin({signin_recognition_mode:'image',signin_image_prompt:'自定义识图提示',signin_image_action:'click'});assert.strictEqual(element('#sRecognitionMode').value,'image');assert.strictEqual(element('#sImagePrompt').value,'自定义识图提示');assert.strictEqual(element('#sImageAction').value,'click');
    editSignin({signin_captcha:1});assert.strictEqual(element('#sRecognitionMode').value,'captcha');
    prepareSignin();element('#sRecognitionMode').value='image';element('#sImagePrompt').value=' ';let sent=0;context.api=async()=>{sent++;return {}};await run("saveAuto('signin')");assert.strictEqual(sent,0);
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
      for(const id of ['#sIsChat','#sClickBtn'])element(id).checked=true;element('#sRecognitionMode').value='captcha';
      let request;context.api=async(p,o)=>{request=[p,o];return {}};await run("saveAuto('signin')");
      assert.strictEqual(request[0],editing?'/api/autos/42':'/api/autos');assert.strictEqual(request[1].method,editing?'PUT':'POST');
      const expected={type:'signin',name:'完整任务',target:'@dummy_bot',command:'/checkin',icon_url:'https://dummy.invalid/icon.png',is_chat:1,win_start:'09:00',win_end:'10:00',click_btn:1,btn_text:'签到',btn_emoji:'✅',click_times:3,click_interval:500,refresh_buttons:0,command_delay:0,button_delay:2.7,result_delay:delay,result_timeout:timeout,success_kw:'成功',fail_kw:'明确失败',points_pat:'积分:(\\d+)',retries:0,signin_recognition_mode:'captcha',signin_image_prompt:run('DEFAULT_SIGNIN_IMAGE_PROMPT'),signin_image_action:'reply',signin_captcha:1};
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
  await test('AI API 已移到设置，自动化 AI 测试卡不包含密钥表单',async()=>{
    const captcha=html.slice(html.indexOf('>AI 测试<'),html.indexOf('id="aCntSign"'));
    const settings=html.slice(html.indexOf('<section id="tab-settings">'),html.indexOf('</section>',html.indexOf('<section id="tab-settings">')));
    for(const id of ['aiProvidersList','aiProviderAdd','aiProviderNote','aiProviderBase','aiProviderKey','aiProviderModel'])assert(settings.includes('id="'+id+'"'));
    assert(settings.includes('type="password" id="aiProviderKey"'));
    assert(!captcha.includes('AI API Key')&&!captcha.includes('aiProviderKey'));
  });
  await test('AI API 列表安全显示备注、状态和输入输出 Token',async()=>{
    context.providerFixture=[{id:'0123456789abcdef',note:'主线路<img>',base:'https://dummy.invalid/v1',model:'dummy',enabled:true,key_present:true,input_tokens:1234,output_tokens:56}];
    run('aiProviders=providerFixture;renderAIProviders()');const out=element('#aiProvidersList').innerHTML;
    assert(out.includes('主线路&lt;img&gt;')&&out.includes('输入 1,234')&&out.includes('输出 56'));
    assert(!out.includes('<img>')&&!out.includes('dummy-provider-secret'));
  });
  await test('新增和编辑 AI API 支持备注且空 Key 沿用',async()=>{
    run('openAIProviderForm()');element('#aiProviderNote').value='主线路';element('#aiProviderBase').value='https://dummy.invalid/v1';element('#aiProviderKey').value='new-key';element('#aiProviderModel').value='dummy';
    const requests=[];context.api=async(p,o)=>{requests.push([p,o]);return {ok:true,provider:{id:'0123456789abcdef'}}};context.loadAIProviders=async()=>{};
    await run('saveAIProvider()');assert.strictEqual(requests[0][0],'/api/ai/providers');assert.strictEqual(JSON.parse(requests[0][1].body).note,'主线路');
    context.providerFixture=[{id:'0123456789abcdef',note:'主线路',base:'https://dummy.invalid/v1',model:'dummy',enabled:true,key_present:true,input_tokens:0,output_tokens:0}];run("aiProviders=providerFixture;openAIProviderForm('0123456789abcdef')");
    assert.strictEqual(element('#aiProviderKey').value,'');assert(element('#aiProviderKey').placeholder.includes('留空不修改'));
  });
  await test('AI API 思考强度保存回填渲染且旧配置默认跟随模型',async()=>{
    const options=html.match(/<select id="aiProviderReasoning"[\s\S]*?<\/select>/)[0];
    for(const effort of ['default','none','low','medium','high','xhigh','max'])assert(options.includes('value="'+effort+'"'));
    context.providerFixture=[{id:'0123456789abcdef',note:'旧线路',base:'https://dummy.invalid/v1',model:'dummy',enabled:true}];
    run('aiProviders=providerFixture;renderAIProviders()');assert(element('#aiProvidersList').innerHTML.includes('思考强度：模型默认'));
    run("openAIProviderForm('0123456789abcdef')");assert.strictEqual(element('#aiProviderReasoning').value,'default');
    context.providerFixture=[{id:'0123456789abcdef',note:'高强度',base:'https://dummy.invalid/v1',model:'dummy',enabled:true,reasoning_effort:'xhigh'}];
    run('aiProviders=providerFixture;renderAIProviders()');assert(element('#aiProvidersList').innerHTML.includes('思考强度：极高（xhigh）'));
    run("openAIProviderForm('0123456789abcdef')");assert.strictEqual(element('#aiProviderReasoning').value,'xhigh');
    let saved;context.api=async(_p,o)=>{saved=JSON.parse(o.body);return {ok:true}};context.loadAIProviders=async()=>{};
    await run('saveAIProvider()');assert.strictEqual(saved.reasoning_effort,'xhigh');
    run('closeAIProviderForm()');assert.strictEqual(element('#aiProviderReasoning').value,'default');
    context.providerFixture=[{id:'fedcba9876543210',note:'切换',base:'https://dummy.invalid/v1',model:'dummy',enabled:true,reasoning_effort:'max'}];run('aiProviders=providerFixture');
    context.api=async(_p,o)=>{saved=JSON.parse(o.body);return {ok:true}};
    await run("mutateAIProvider('fedcba9876543210','toggle')");assert.strictEqual(saved.reasoning_effort,'max');assert.strictEqual(saved.enabled,false);
  });
  await test('单条 AI API 测试结果显示在对应线路卡片并展示回复耗时和错误',async()=>{
    context.providerFixture=[{id:'0123456789abcdef',note:'主线路',base:'https://dummy.invalid/v1',model:'dummy',enabled:true},{id:'fedcba9876543210',note:'备用线路',base:'https://dummy2.invalid/v1',model:'dummy2',enabled:true}];run('aiProviders=providerFixture;renderAIProviders()');
    assert.strictEqual(run('Object.keys(aiTestResults).length'),0);run('renderAIProviders()');
    context.api=async(p)=>{assert.strictEqual(p,'/api/ai/providers/0123456789abcdef/test');return {ok:true,reply:'测试成功',elapsed_ms:27}};context.loadAIProviders=async()=>{};
    await run("mutateAIProvider('0123456789abcdef','test')");
    let results=run('JSON.stringify(aiTestResults)');assert(results.includes('0123456789abcdef')&&results.includes('主线路')&&results.includes('连接测试成功')&&results.includes('27 ms'));assert(!results.includes('fedcba9876543210'));
    context.api=async()=>({ok:false,reply:'<img>',error:'失败 <script>',elapsed_ms:9});await run("mutateAIProvider('0123456789abcdef','test')");
    results=run('JSON.stringify(aiTestResults)');assert(results.includes('<img>')&&results.includes('失败 <script>'));
    assert(run('aiTestResults["0123456789abcdef"].includes("主线路")'));assert(!run('aiTestResults["fedcba9876543210"]'));
  });
  await test('被任务引用的 AI API 停用删除错误正常显示',async()=>{
    context.providerFixture=[{id:'0123456789abcdef',note:'被引用线路',base:'https://dummy.invalid/v1',model:'dummy',enabled:true}];run('aiProviders=providerFixture');context.window.confirm=()=>true;context.api=async()=>{throw new Error('仍被任务引用，无法停用或删除（409）')};
    await run("mutateAIProvider('0123456789abcdef','toggle')");assert(element('#aiTestResult').textContent.includes('409'));assert(element('#aiTestResult').textContent.includes('仍被任务引用'));
    await run("mutateAIProvider('0123456789abcdef','delete')");assert(element('#aiTestResult').textContent.includes('409'));assert.strictEqual(run('aiProviders.length'),1);context.window.confirm=()=>false;
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
      assert.deepStrictEqual(JSON.parse(request[1].body),{image:sampleImage,length:Number(length),mode:'captcha',provider_ids:'',ai_enabled:'0',ocr_enabled:'1'});
      const out=element('#captchaTestResult').textContent;
      assert(out.includes('识别成功')&&out.includes('aB42')&&out.includes('本地 OCR')&&out.includes('48 ms'));
      assert(!element('#captchaTestBtn').disabled);
    }
    configureAI(true,false);element('#captchaTestLength').value='4';
    context.api=async(p,o)=>{request=JSON.parse(o.body);return {ok:true,text:'1234',channel:'ai',elapsed_ms:5}};
    await run('testCaptchaRecognition()');assert.strictEqual(request.ai_enabled,'1');assert.strictEqual(request.ocr_enabled,'0');
    assert(element('#captchaTestResult').textContent.includes('使用通道：AI'));
  });
  await test('自动化 AI 测试可切换图片识别、自定义提示词并选择 API',async()=>{
    assert(html.includes('>AI 测试<')&&html.includes('data-ai-test-mode="captcha"')&&html.includes('data-ai-test-mode="image"'));
    context.providerFixture=[{id:'0123456789abcdef',note:'主线路',enabled:true},{id:'aaaaaaaaaaaaaaaa',note:'停用线路',enabled:false}];run("aiProviders=providerFixture;aiTestProviderSel=new Set(['aaaaaaaaaaaaaaaa']);renderAITestProviders()");assert.strictEqual(run('aiTestProviderSel.size'),0);assert(!element('#aiTestProviders').innerHTML.includes('停用线路'));
    selectImage();run("setAITestMode('image');aiTestProviderSel=new Set(['0123456789abcdef'])");element('#aiTestImagePrompt').value='识别并只输出物体名称';let body;
    context.api=async(_p,o)=>{body=JSON.parse(o.body);return {ok:true,text:'苹果',channel:'ai',errors:[],elapsed_ms:12}};
    await run('testCaptchaRecognition()');assert.deepStrictEqual(body,{image:sampleImage,length:0,mode:'image',prompt:'识别并只输出物体名称',options:'',provider_ids:'0123456789abcdef'});
    assert(element('#captchaTestResult').textContent.includes('图片答案：苹果'));assert(element('#captchaTestResult').textContent.includes('12 ms'));
    element('#aiTestImageAction').value='click';element('#aiTestImageOptions').value='苹果\n香蕉';context.api=async(_p,o)=>{body=JSON.parse(o.body);return {ok:true,text:'苹果',channel:'ai',errors:[],elapsed_ms:8}};await run('testCaptchaRecognition()');assert.strictEqual(body.options,'苹果\n香蕉');assert(element('#captchaTestResult').textContent.includes('匹配选项：苹果'));
    run("setAITestMode('captcha');aiTestProviderSel.clear()");
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
    context.api=apiContext.api;selectImage();await run('testCaptchaRecognition()');
    assert(element('#captchaTestResult').textContent.includes('mock HTTP 参数错误'));assert(!element('#captchaTestBtn').disabled);
    apiContext.fetch=async()=>({status:401});selectImage();await run('testCaptchaRecognition()');assert.strictEqual(gated,1);
    assert(element('#captchaTestResult').textContent.includes('未登录面板'));
  });
  await test('签到日志显示来源和失败原因且不执行服务端文本',async()=>{
    context.LOG_KINDS={signin:'签到'};context.fmt=()=>'';
    run(slice('let logOpenIds=new Set();','let logQ='));
    context.logFixture={action:'signin',ok:0,ts:1,chat_id:'@dummy',detail:{task:'fixture',command:'/start',error:'<img src=x onerror=bad()>',captcha_message_id:102,captcha_prompt:'请输入测试信息',command_delay:0,button_delay:3,refresh_buttons:0}};
    const markup=run('logRow(logFixture)');
    assert(markup.includes('失败原因'));assert(markup.includes('图片来源'));assert(markup.includes('消息 #102'));
    assert(markup.includes('命令后等待'));assert(markup.includes('0 秒'));assert(markup.includes('复用同一按钮'));
    assert(markup.includes('&lt;img'));assert(!markup.includes('<img'));
  });
  await test('定时抢注日志区分历史按钮和真实发送指令',async()=>{
    context.logFixture={action:'scheduled_register',ok:0,ts:1,chat_id:'@dummy',content:'/start',detail:{task:'抢注',status:'unconfirmed',button_source:'history',test_only:true,command:'/start',error:'未收到指定内容'}};
    const history=run('logRow(logFixture)');assert(history.includes('历史卡片按钮'));assert(history.includes('测试流程'));assert(!history.includes('已发送指令'));
    context.logFixture.detail={...context.logFixture.detail,button_source:'command',test_only:false};const command=run('logRow(logFixture)');assert(command.includes('发送指令'));assert(command.includes('已发送指令'));
    delete context.logFixture.detail.button_source;const legacy=run('logRow(logFixture)');assert(legacy.includes('配置指令'));assert(!legacy.includes('已发送指令'));
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
  const mockConfig=(name='纯配置')=>({format:'tg-persona-config',schema_version:1,account:{id:'default',label:'默认'},automations:[{type:'custom',name,target:'@dummy',custom_trigger:'{"mode":"manual"}',custom_steps:'[{"t":"send","to":"target","text":"/start","reply":false}]',enabled:1}],rules:[],watched_groups:[]});
  function useConfig(data=mockConfig()){q('#configFile').files=[{name:'backup.json',type:'application/json',size:1024}];ex('readConfigFile()');readers.at(-1).complete(JSON.stringify(data))}
  function resetBatch(kind='window'){ex('batchBusy=false;selectedAutoIds=new Set([1,2]);invalidateBatchPreview()');q('#batchKind').value=kind;q('#batchStart').value='08:00';q('#batchEnd').value='12:00';q('#batchEvery').value='1';q('#batchUnit').value='60'}
  await test('自定义表单只提供最新六类步骤、严格JSON和移动触控约束',async()=>{
    const form=html.slice(html.indexOf('id="af-custom"'),html.indexOf('id="af-keepalive"'));
    for(const type of ['send','wait','click','wait_reply','extract','captcha'])assert(form.includes("fillStep('"+type+"')"));
    for(const legacy of ["fillStep('until')","fillStep('manual')",'旧版配置无损保留'])assert(!form.includes(legacy));
    assert(form.includes('custom_trigger')===false);assert(form.includes('严格格式原始 JSON'));assert(html.includes('viewport-fit=cover'));assert(html.includes('#af-custom input:not([type=checkbox])'));
  });
  await test('手动、秒级定时和监听群触发完整往返',async()=>{
    ex("setCustomTrigger('{\"mode\":\"manual\"}')");assert.deepStrictEqual(copy(ex('buildCustomTrigger()')),{mode:'manual'});
    const schedule={mode:'schedule',at:'2026-12-03T09:08:07'};app.fixture=schedule;ex('setCustomTrigger(JSON.stringify(fixture))');assert.strictEqual(q('#cTriggerDate').value,'2026-12-03');assert.strictEqual(q('#cTriggerTime').value,'09:08');assert.strictEqual(q('#cTriggerSecond').value,'07');assert.deepStrictEqual(copy(ex('buildCustomTrigger()')),schedule);
    const group={mode:'group',groups:['-1001','-1002'],sender:'bot',logic:'all',text:'开放.*注册',text_regex:true,button:'立即注册',button_regex:false,cooldown:45};app.fixture=group;ex('setCustomTrigger(JSON.stringify(fixture))');assert.deepStrictEqual(copy(ex('[...autoGroupSel.custom]')),group.groups);assert.deepStrictEqual(copy(ex('buildCustomTrigger()')),group);
    assert(!q('#cTriggerGroupFields').classList.contains('hidden'));assert(q('#cTriggerScheduleFields').classList.contains('hidden'));
  });
  await test('六类步骤卡展示全部新类型，秒数精确转成毫秒并可排序删除',async()=>{
    ex("setVisualSteps('[]')");for(const type of ['send','wait','click','wait_reply','extract','captcha'])ex("fillStep('"+type+"')");
    let list=JSON.parse(q('#cSteps').value);assert.deepStrictEqual(list.map(step=>step.t),['send','wait','click','wait_reply','extract','captcha']);
    assert.deepStrictEqual(q('#stepCards').children.map(card=>card.children[0].textContent),['1. 发送消息','2. 等待','3. 点击按钮','4. 等待回复','5. 提取内容','6. 识别验证码']);
    const waitInput=q('#stepCards').children[1].children[1].children[0];waitInput.value='0.125';waitInput.listeners.input();assert.strictEqual(JSON.parse(q('#cSteps').value)[1].ms,125);
    ex('moveStep(1,1)');assert.strictEqual(JSON.parse(q('#cSteps').value)[2].ms,125);ex('removeStep(2)');assert.strictEqual(JSON.parse(q('#cSteps').value).length,5);
  });
  await test('严格步骤校验逐步编号并拒绝旧字段、错误类型和上限',async()=>{
    assert(ex("stepIssue([{t:'wait',ms:-1}],true)").startsWith('第 1 步：'));
    assert(ex("stepIssue([{t:'send',to:'',text:'',reply:false}],true)").includes('第 1 步：请填写'));
    assert(ex("stepIssue([{t:'wait',ms:0,v:0}],true)").includes('第 1 步：包含未知字段 v'));
    assert(ex("stepIssue([{t:'captcha',from:'last.text',length:'4',send_to:'',template:'{code}'}],true)").includes('第 1 步：数值字段必须是整数'));
    assert(ex("stepIssue([{...STEP_HINTS.click,timeout_ms:0}],true)").includes('等待上限'));
    assert(ex("stepIssue([{...STEP_HINTS.extract,regex:'(.)',save:'bad name'}],true)").includes('变量名'));
    assert(ex("stepIssue([{...STEP_HINTS.click,ai:true,text:'{credential.secret}'}],true)").includes('AI 选按钮'));
    assert(ex("triggerIssue({mode:'schedule',at:'2026-02-30T09:00:00'})").includes('日期无效'));
    assert.throws(()=>ex("parseSteps(JSON.stringify(Array.from({length:51},()=>({t:'wait',ms:0}))))"),/50/);
    ex("setVisualSteps(JSON.stringify(Array.from({length:50},()=>({t:'wait',ms:0}))));fillStep('wait')");assert.strictEqual(JSON.parse(q('#cSteps').value).length,50);
  });
  await test('三个模板都确认替换，注册链接模板使用触发变量和凭证',async()=>{
    ex("setVisualSteps(JSON.stringify([{t:'wait',ms:1}]))");q('#stepTemplate').value='basic';const before=q('#cSteps').value;app.window.confirm=()=>false;ex('applyStepTemplate()');assert.strictEqual(q('#cSteps').value,before);
    app.window.confirm=()=>true;ex('applyStepTemplate()');assert.deepStrictEqual(JSON.parse(q('#cSteps').value).map(step=>step.t),['send','wait','click','wait_reply']);assert.deepStrictEqual(copy(ex('buildCustomTrigger()')),{mode:'manual'});
    q('#stepTemplate').value='registration';ex('applyStepTemplate()');const registration=q('#cSteps').value;assert(registration.includes('{trigger.bot}')&&registration.includes('{trigger.payload}')&&registration.includes('{credential.user}')&&registration.includes('{credential.secret}'));assert.strictEqual(copy(ex('buildCustomTrigger()')).mode,'group');assert.strictEqual(q('#cTarget').value,'{trigger.bot}');
    q('#stepTemplate').value='scheduled';ex('applyStepTemplate()');assert.strictEqual(copy(ex('buildCustomTrigger()')).mode,'schedule');assert(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/.test(copy(ex('buildCustomTrigger()')).at));
  });
  await test('自定义任务精确保存触发与步骤，凭证只通过独立POST发送',async()=>{
    ex("cancelAutoEdit('custom');editAutoAccount=accountContext()");q('#cName').value='凭证流程';q('#cTarget').value='{trigger.bot}';q('#cRetry').value='2';q('#cUser').value='测试用户';q('#cSecret').value='test-secret';
    const trigger={mode:'group',groups:[],sender:'any',logic:'any',text:'',text_regex:false,button:'注册',button_regex:false,cooldown:30};const steps=[{t:'send',to:'{trigger.bot}',text:'/start {trigger.payload}',reply:false},{t:'send',to:'{trigger.bot}',text:'{credential.user}',reply:false},{t:'send',to:'{trigger.bot}',text:'{credential.secret}',reply:false}];app.triggerFixture=trigger;app.stepsFixture=steps;ex('setCustomTrigger(JSON.stringify(triggerFixture));setVisualSteps(JSON.stringify(stepsFixture))');
    const requests=[];app.api=async(p,o)=>{requests.push([p,JSON.parse(o.body)]);return {ok:true,id:88}};app.loadAutos=async()=>{};await ex("saveAuto('custom')");
    assert.deepStrictEqual(requests,[['/api/autos',{type:'custom',name:'凭证流程',target:'{trigger.bot}',custom_trigger:JSON.stringify(trigger),custom_steps:JSON.stringify(steps),chain_retry:2,retries:2,enabled:0}],['/api/autos/88/link-credential',{user:'测试用户',secret:'test-secret'}]]);
  });
  await test('自定义任务编辑、空凭证保留、重置及恶意文本均完整安全',async()=>{
    const trigger={mode:'schedule',at:'2026-12-03T09:08:07'},steps=[{t:'send',to:'@dummy',text:'<img src=x onerror=bad()>',reply:true},{t:'wait_reply',from:'@dummy',success:'完成',fail:'失败',regex:false,timeout_ms:1234,save:'reply'}];app.customTask={id:44,type:'custom',name:'编辑任务',target:'@dummy',custom_trigger:JSON.stringify(trigger),custom_steps:JSON.stringify(steps),chain_retry:3,retries:1,enabled:1,link_credential_set:true};
    ex('autos=[customTask];editAutoItem(44)');assert.deepStrictEqual(copy(ex('buildCustomTrigger()')),trigger);assert.deepStrictEqual(JSON.parse(q('#cSteps').value),steps);assert.strictEqual(q('#cUser').value,'');assert.strictEqual(q('#cSecret').value,'');assert.strictEqual(q('#stepCards').children[0].children[2].children[0].value,steps[0].text);
    const requests=[];app.api=async(p,o)=>{requests.push([p,JSON.parse(o.body)]);return {ok:true}};app.loadAutos=async()=>{};await ex("saveAuto('custom')");assert.deepStrictEqual(requests,[['/api/autos/44',{type:'custom',name:'编辑任务',target:'@dummy',custom_trigger:JSON.stringify(trigger),custom_steps:JSON.stringify(steps),chain_retry:3,retries:1,enabled:1}]]);
    ex("cancelAutoEdit('custom')");assert.deepStrictEqual(copy(ex('buildCustomTrigger()')),{mode:'manual'});assert.strictEqual(q('#cSteps').value,'[]');assert.strictEqual(q('#cTarget').value,'');assert.strictEqual(ex('autoGroupSel.custom.size'),0);
  });
  await test('启用中的自定义任务首次添加凭据会先停用再独立保存凭据',async()=>{
    const trigger={mode:'group',groups:[],sender:'any',logic:'any',text:'注册',text_regex:false,button:'',button_regex:false,cooldown:0},steps=[{t:'send',to:'{trigger.bot}',text:'{credential.user} {credential.secret}',reply:false}];
    app.customTask={id:45,type:'custom',name:'首次凭据',target:'{trigger.bot}',custom_trigger:JSON.stringify(trigger),custom_steps:JSON.stringify(steps),chain_retry:1,retries:2,enabled:1,link_credential_set:false};ex('autos=[customTask];editAutoItem(45)');q('#cUser').value='user';q('#cSecret').value='secret';
    const requests=[];app.api=async(p,o)=>{requests.push([p,JSON.parse(o.body)]);return {ok:true}};app.loadAutos=async()=>{};await ex("saveAuto('custom')");assert.strictEqual(requests[0][1].enabled,0);assert.deepStrictEqual(requests[1],['/api/autos/45/link-credential',{user:'user',secret:'secret'}]);
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
    q('#logList').innerHTML='旧日志';q('#aiProviderKey').value='mock-secret';q('#aiProviderForm').classList.remove('hidden');q('#tgCard').innerHTML='旧手机号';ex('resetAccountCaches()');
    for(const name of ['autos','allGroups','allRules','watchedGroups'])assert.strictEqual(ex(name+'.length'),0);assert.strictEqual(ex('selectedAutoIds.size'),0);assert.strictEqual(ex('editAuto'),null);assert.strictEqual(ex('editingRule'),null);assert.strictEqual(ex('state.logged_in'),false);assert.strictEqual(q('#aiProviderKey').value,'');assert(q('#aiProviderForm').classList.contains('hidden'));assert.strictEqual(q('#cSteps').value,'[]');assert.strictEqual(ex('logQ'),'');assert.strictEqual(q('#autoSelectedCount').textContent,'已选 0 项');
    const loaders=['loadState','loadSettings','loadAutos','loadRules','loadRuleGroups','loadDialogs','loadLogs','renderTgCard'];for(const name of loaders)app[name]=()=>{};
    ex("switchAccount('account-new')");assert.strictEqual(store.get('tgp_selected_id'),'account-new');assert(![...store.keys()].some(k=>/key|phone|session/.test(k)));
  });
  await test('状态、任务缓存和TG表单过期响应不覆盖当前账号',async()=>{
    const clean=vm.createContext({...app,fetch(){throw new Error('禁止网络')},localStorage:{getItem:()=>null,setItem(){}},document:fullDocument,window:{confirm:()=>false},setTimeout(){},clearTimeout(){}});vm.runInContext(js.replace(/\nbootstrap\(\);\s*$/,''),clean);const cx=s=>vm.runInContext(s,clean);
    for(const fn of ['loadState','loadAutos','loadSettings']){let done;clean.api=()=>new Promise(r=>done=r);const pending=cx(fn+'()');cx("selectedAccount='other';accountEpoch++;state={tg:{},logged_in:false};autos=[]");done(fn==='loadState'?{tg:{me:{name:'迟到client'}},logged_in:true,stats:{}}:fn==='loadAutos'?{autos:[{id:42,name:'迟到任务'}]}:{api_id:123456,api_hash:'mock-old-hash'});await pending;assert.strictEqual(cx('state.logged_in'),false);assert.strictEqual(cx('autos.length'),0);assert.notStrictEqual(q('#tgApiHash').value,'mock-old-hash');cx("selectedAccount='default';accountEpoch++")}
  });
  await test('账号标签纯文本，默认不可删，其他账号两次确认，管理错误恢复按钮',async()=>{
    ex("accounts=[{id:'default',label:'<img src=x>',logged_in:false},{id:'work',label:'工作',logged_in:true}];selectedAccount='default';renderAccounts()");assert.strictEqual(q('#accountTop').children[0].textContent,'<img src=x> · 未登录');assert(q('#accountDelete').disabled);
    let sent=0;app.api=async()=>{sent++;return {}};await ex("manageAccount('delete')");assert.strictEqual(sent,0);
    ex("selectedAccount='work'");let prompts=0;app.window.confirm=()=>++prompts===1;await ex("manageAccount('delete')");assert.strictEqual(prompts,2);assert.strictEqual(sent,0);
    q('#accountLabel').value='重命名';app.api=async()=>{throw new Error('mock账号冲突')};await ex("manageAccount('rename')");assert(!q('#accountRename').disabled);assert(q('#accountResult').textContent.includes('mock账号冲突'));
  });
  await test('账号管理和登录合并，共用API只保存一次',async()=>{
    assert.strictEqual((html.match(/id="foldTg"/g)||[]).length,1);assert.strictEqual((html.match(/id="tgCard"/g)||[]).length,1);
    assert(html.includes('添加账号并登录'));assert(html.includes('所有 Telegram 账号共用这一套 API'));assert(!html.includes('Telegram 多账号'));
    q('#tgApiId').value='12345';q('#tgApiHash').value='dummy-shared-api-hash';const requests=[];
    app.api=async(p,o)=>{requests.push([p,o]);return {}};await ex('saveTgApi()');
    assert.deepStrictEqual(requests.map(x=>x[0]),['/api/settings']);assert.deepStrictEqual(JSON.parse(requests[0][1].body),{api_id:'12345',api_hash:'dummy-shared-api-hash'});
  });
  await test('填写备注添加账号后自动切换并进入手机号登录',async()=>{
    const oldSwitch=app.switchAccount,oldLoad=app.loadAccounts,oldRender=app.renderTgCard;let switched='',rendered=0;
    app.switchAccount=id=>{switched=id};app.loadAccounts=async()=>{};app.renderTgCard=async()=>{rendered++};app.api=async(p,o)=>{assert.strictEqual(p,'/api/accounts');assert.strictEqual(JSON.parse(o.body).label,'测试小号');return {id:'new-account'}};
    q('#accountLabel').value='测试小号';await ex("manageAccount('create')");assert.strictEqual(switched,'new-account');assert.strictEqual(rendered,1);assert(q('#accountResult').textContent.includes('请输入手机号'));
    app.switchAccount=oldSwitch;app.loadAccounts=oldLoad;app.renderTgCard=oldRender;
  });
  await test('定时抢注测试按钮真实调用测试接口但不调用立即执行',async()=>{
    ex("selectedAccount='default';accountEpoch++;autos=[{id:77,type:'scheduled_register',name:'测试抢注',enabled:1}];autoTestBusy.clear()");const requests=[];
    app.window.confirm=()=>true;app.api=async(p,o)=>{requests.push([p,o]);return {ok:true,tested:true}};app.loadAutos=async()=>{};
    await ex('testAuto(77)');assert.deepStrictEqual(requests.map(x=>x[0]),['/api/autos/77/test']);assert.strictEqual(requests[0][1].method,'POST');assert.strictEqual(ex('autoTestBusy.size'),0);app.window.confirm=()=>false;
  });
  await test('群链接抢注表单与凭据分离保存，任务体不含安全码',async()=>{
    const form=html.slice(html.indexOf('id="af-link_register"'),html.indexOf('id="af-custom"'));
    for(const id of ['lkName','lkGroups','lkButton','lkReady','lkExisting','lkUser','lkSecret','lkMode','lkLimit','lkWwait','lkSuccess','lkFail','lkGroupReply','lkGroupReplyText','lkGroupReplyDelay','lkTimeout'])assert(form.includes('id="'+id+'"'));
    assert(!form.includes('id="lkTarget"'));assert(form.includes('start=服务名-天数-Register_代码'));
    assert(form.includes('type="password"')&&form.includes('串行接力')&&form.includes('随机一条'));
    ex("selectedAccount='default';accountEpoch++;editAuto=null;editAutoAccount=null;cancelAutoEdit('link_register')");
    assert.strictEqual(q('#lkAiFlock').checked,false);assert.strictEqual(q('#lkAiFlockMinutes').value,'5');assert.strictEqual(q('#lkAiFlockPrompt').value,ex('DEFAULT_REGISTRATION_AI_FLOCK_PROMPT'));assert.deepStrictEqual(['6','120','3000','200'].map((v,i)=>q('#lkAiFlock'+['ContextMessages','MessageChars','SkillChars','MaxTokens'][i]).value),['6','120','3000','200']);assert(ex('DEFAULT_REGISTRATION_AI_FLOCK_PROMPT').includes('NO_REPLY'));assert.strictEqual((html.match(/只输出聊天内容或NO_REPLY。<\/textarea>/g)||[]).length,3);
    app.providerFixture=[{id:'0123456789abcdef',note:'AI',enabled:true},{id:'fedcba9876543210',note:'备用',enabled:true}];ex("aiProviders=providerFixture;registrationAIProviderSel.lk=new Set(['0123456789abcdef','fedcba9876543210'])");q('#lkAiFlock').checked=true;q('#lkAiFlockPrompt').value='注册成功人格';q('#lkAiFlockReaction').value='👍';
    q('#lkName').value='链接抢注';q('#lkButton').value='注册';
    q('#lkReady').value='已进入注册状态';q('#lkExisting').value='已有号';q('#lkSuccess').value='注册成功';q('#lkUser').value='测试用户';q('#lkSecret').value='测试安全码';
    q('#lkGroupReply').checked=true;q('#lkGroupReplyText').value='拿下';q('#lkGroupReplyDelay').value='7';
    q('#lkMode').value='1';q('#lkLimit').value='3';q('#lkWwait').value='6';q('#lkTimeout').value='30';
    const requests=[];app.api=async(p,o)=>{requests.push([p,o]);return p==='/api/autos'?{ok:true,id:88}:{ok:true}};app.loadAutos=async()=>{};
    await ex("saveAuto('link_register')");
    assert.deepStrictEqual(requests.map(x=>x[0]),['/api/autos','/api/autos/88/link-credential']);
    const body=JSON.parse(requests[0][1].body);
    assert(!('lkUser' in body)&&!('link_secret' in body)&&!JSON.stringify(body).includes('测试安全码'));
    assert.deepStrictEqual(body,{type:'link_register',name:'链接抢注',groups:'',btn_text:'注册',keywords:'已进入注册状态',success_kw:'注册成功',fail_kw:'',link_mode:1,link_limit:3,link_wait:6,result_timeout:30,retries:0,existing_kw:'已有号',success_reply_enabled:1,success_reply_text:'拿下',success_reply_delay:7,ai_flock_enabled:1,ai_flock_minutes:5,ai_flock_prompt:'注册成功人格',ai_flock_provider_ids:'0123456789abcdef,fedcba9876543210',ai_flock_reaction:'👍',ai_flock_context_messages:6,ai_flock_message_chars:120,ai_flock_skill_chars:3000,ai_flock_max_tokens:200,ai_flock_knowledge_chars:2000,ai_flock_knowledge_on_demand:0});
    assert.deepStrictEqual(JSON.parse(requests[1][1].body),{user:'测试用户',secret:'测试安全码'});
  });
  await test('群链接抢注测试按钮调用测试接口',async()=>{
    ex("selectedAccount='default';accountEpoch++;autos=[{id:88,type:'link_register',name:'链接抢注',enabled:1}];autoTestBusy.clear()");const requests=[];
    app.window.confirm=()=>true;app.api=async(p,o)=>{requests.push([p,o]);return {ok:true,tested:true}};app.loadAutos=async()=>{};
    await ex('testAuto(88)');assert.deepStrictEqual(requests.map(x=>x[0]),['/api/autos/88/test']);assert.strictEqual(ex('autoTestBusy.size'),0);app.window.confirm=()=>false;
  });
  await test('群按钮开注使用实测模板保存触发条件和成功群回复',async()=>{
    const form=html.slice(html.indexOf('id="af-button_register"'),html.indexOf('id="af-link_register"'));
    assert(form.includes('brAnnouncement')&&form.includes('brAnnouncementButton'));
    assert(!form.includes('history_first')&&!form.includes('关闭弹窗重试'));
    ex("selectedAccount='default';accountEpoch++;cancelAutoEdit('button_register')");
    assert.strictEqual(q('#brGroupReply').checked,true);assert.strictEqual(q('#brCommand').value,'/start');
    assert.strictEqual(q('#brAnnouncement').value,'定时注册');assert.strictEqual(q('#brButton').value,'创建');
    assert.strictEqual(q('#brReady').value,'注册状态');assert.strictEqual(q('#brSuccess').value,'成功');
    assert.strictEqual(q('#brReplyText').value,'拿下！');assert.strictEqual(q('#brReplyDelay').value,'5');
    q('#brName').value='Bot 公告';q('#brUser').value='tester';q('#brSecret').value='offline-secret';ex("registrationAIProviderSel.br=new Set(['0123456789abcdef'])");q('#brAiFlockReaction').value='🔥';q('#brAiFlockContextMessages').value='1';q('#brAiFlockMessageChars').value='40';q('#brAiFlockSkillChars').value='0';q('#brAiFlockMaxTokens').value='50';
    const requests=[];app.api=async(p,o)=>{requests.push([p,JSON.parse(o.body)]);return {ok:true,id:99}};app.loadAutos=async()=>{};
    await ex("saveAuto('button_register')");
    assert.strictEqual(requests[0][0],'/api/autos');assert.strictEqual(requests[0][1].type,'button_register');
    assert.strictEqual(requests[0][1].command,'/start');assert.strictEqual(requests[0][1].announcement_button,'点击注册');
    assert.strictEqual(requests[0][1].success_reply_enabled,1);assert.strictEqual(requests[0][1].success_reply_text,'拿下！');
    assert.strictEqual(requests[0][1].ai_flock_provider_ids,'0123456789abcdef');assert.strictEqual(requests[0][1].ai_flock_reaction,'🔥');
    assert.deepStrictEqual([requests[0][1].ai_flock_context_messages,requests[0][1].ai_flock_message_chars,requests[0][1].ai_flock_skill_chars,requests[0][1].ai_flock_max_tokens],[1,40,0,50]);
    assert.strictEqual(requests[0][1].enabled,0);
    assert(!JSON.stringify(requests[0]).includes('offline-secret'));
    assert.strictEqual(requests[1][0],'/api/autos/99/link-credential');
  });
  await test('群按钮开注编辑回填并保存原群回复、延迟和自定义指令',async()=>{
    ex("selectedAccount='default';accountEpoch++;autos=[{id:99,type:'button_register',name:'公告',announcement_kw:'开放注册',announcement_button:'进入',command:'/register',btn_text:'注册',keywords:'注册状态',success_kw:'创建成功',existing_kw:'已有号',enabled:1,link_credential_set:true,success_reply_enabled:1,success_reply_text:'拿下',success_reply_delay:0,ai_flock_context_messages:9,ai_flock_message_chars:222,ai_flock_skill_chars:4567,ai_flock_max_tokens:333}];editAutoItem(99)");
    assert.strictEqual(q('#brCommand').value,'/register');assert.strictEqual(q('#brReplyDelay').value,'0');assert(q('#brGroupReply').checked);
    assert(!q('#brGroupReplyFields').classList.contains('hidden'));assert.strictEqual(q('#brSecret').value,'');assert.deepStrictEqual(['ContextMessages','MessageChars','SkillChars','MaxTokens'].map(s=>q('#brAiFlock'+s).value),['9','222','4567','333']);
    const requests=[];app.api=async(p,o)=>{requests.push([p,JSON.parse(o.body)]);return {ok:true}};
    await ex("saveAuto('button_register')");assert.strictEqual(requests.length,1);
    assert.strictEqual(requests[0][0],'/api/autos/99');assert.strictEqual(requests[0][1].command,'/register');
    assert.strictEqual(requests[0][1].success_reply_text,'拿下');assert.strictEqual(requests[0][1].success_reply_delay,0);
    assert.deepStrictEqual([requests[0][1].ai_flock_context_messages,requests[0][1].ai_flock_message_chars,requests[0][1].ai_flock_skill_chars,requests[0][1].ai_flock_max_tokens],[9,222,4567,333]);
    assert.strictEqual(requests[0][1].enabled,1);
  });
  await test('兑换码抢注只能选一个群，保存固定Bot和现有接力配置',async()=>{
    const select=html.match(/<select\b[^>]*id="crGroup"[^>]*>/)[0];
    assert(!select.includes('multiple'));assert(select.includes('required'));
    ex("selectedAccount='default';accountEpoch++;cancelAutoEdit('code_register')");
    assert.strictEqual(q('#crGroup').value,'');assert.strictEqual(q('#crMode').value,'1');
    assert.strictEqual(q('#crWait').value,'3');assert.strictEqual(q('#crExisting').value,'无法启用续期功能');
    assert.strictEqual(q('#crGroupReply').checked,false);
    q('#crName').value='兑换码任务';q('#crUser').value='tester';q('#crSecret').value='test-secret';ex("registrationAIProviderSel.cr=new Set(['fedcba9876543210'])");q('#crAiFlockReaction').value='👏';q('#crAiFlockContextMessages').value='50';q('#crAiFlockMessageChars').value='2000';q('#crAiFlockSkillChars').value='200000';q('#crAiFlockMaxTokens').value='8192';
    const requests=[];app.api=async(p,o)=>{requests.push([p,JSON.parse(o.body)]);return {ok:true,id:100}};app.loadAutos=async()=>{};
    await ex("saveAuto('code_register')");assert.strictEqual(requests.length,0);
    q('#crGroup').value='-100123';q('#crBot').value='@fixed_bot';
    await ex("saveAuto('code_register')");
    assert.strictEqual(requests[0][0],'/api/autos');const body=requests[0][1];
    assert.strictEqual(body.type,'code_register');assert.strictEqual(body.groups,'-100123');assert.strictEqual(body.target,'@fixed_bot');
    assert.strictEqual(body.link_limit,4);assert.strictEqual(body.link_mode,1);assert.strictEqual(body.link_wait,3);
    assert.strictEqual(body.ai_flock_provider_ids,'fedcba9876543210');assert.strictEqual(body.ai_flock_reaction,'👏');
    assert.deepStrictEqual([body.ai_flock_context_messages,body.ai_flock_message_chars,body.ai_flock_skill_chars,body.ai_flock_max_tokens],[50,2000,200000,8192]);
    assert.strictEqual(body.enabled,0);assert(!JSON.stringify(body).includes('test-secret'));
    assert.strictEqual(requests[1][0],'/api/autos/100/link-credential');
  });
  await test('兑换码编辑保留单群Bot和零延迟，不回填安全码',async()=>{
    ex("selectedAccount='default';accountEpoch++;watchedGroups=[{chat_id:'-100456',name:'有效群'}];watchedGroupsLoaded=true;autos=[{id:100,type:'code_register',name:'兑换码',groups:'-100456',target:'@another_bot',btn_text:'注册',keywords:'注册状态',success_kw:'成功',enabled:1,link_credential_set:true,link_mode:0,link_limit:4,link_wait:3,success_reply_enabled:1,success_reply_text:'拿下',success_reply_delay:0}];editAutoItem(100)");
    assert.strictEqual(q('#crGroup').value,'-100456');assert.strictEqual(q('#crBot').value,'@another_bot');
    assert.strictEqual(q('#crSecret').value,'');assert.strictEqual(q('#crReplyDelay').value,'0');
    const requests=[];app.api=async(p,o)=>{requests.push([p,JSON.parse(o.body)]);return {ok:true}};
    await ex("saveAuto('code_register')");assert.strictEqual(requests.length,1);
    assert.strictEqual(requests[0][0],'/api/autos/100');assert.strictEqual(requests[0][1].groups,'-100456');
    assert.strictEqual(requests[0][1].success_reply_delay,0);assert.strictEqual(requests[0][1].enabled,1);
  });
  await test('三种注册成功水群限制控件范围一致，重置恢复默认且非法整数不请求',async()=>{
    const specs=[['ContextMessages','6','1','50'],['MessageChars','120','40','2000'],['SkillChars','3000','0','200000'],['MaxTokens','200','50','8192']];
    for(const prefix of ['br','lk','cr'])for(const [suffix,def,min,max] of specs){const input=html.match(new RegExp('<input\\b[^>]*id="'+prefix+'AiFlock'+suffix+'"[^>]*>'))[0];for(const attr of ['value="'+def+'"','min="'+min+'"','max="'+max+'"','step="1"'])assert(input.includes(attr))}
    let sent=0;app.api=async()=>{sent++;return {}};for(const [type,prefix] of [['button_register','br'],['link_register','lk'],['code_register','cr']]){ex("cancelAutoEdit('"+type+"')");q('#'+prefix+'AiFlockContextMessages').value='1.5';await ex("saveAuto('"+type+"')");assert(q('#toast').textContent.includes('整数'));assert.strictEqual(sent,0);ex("cancelAutoEdit('"+type+"')");assert.deepStrictEqual(specs.map(([suffix])=>q('#'+prefix+'AiFlock'+suffix).value),specs.map(x=>x[1]))}
  });
  await test('四个AI水群表情选择器共享真实按钮并支持选择清除编辑重置',async()=>{
    ex('renderAIFlockReactionPickers()');const fields=['waterAiJobReaction','brAiFlockReaction','lkAiFlockReaction','crAiFlockReaction'];for(const field of fields){const box=q('#'+field+'Picker');assert.strictEqual(box.children.length,ex('EMOJIS.length'));const first=box.children[0];assert.strictEqual(first.type,'button');assert.strictEqual(first.getAttribute('aria-pressed'),'false');assert(!first.attributes.onclick);ex(`toggleAIFlockReaction('${field}','👍')`);assert.strictEqual(q('#'+field).value,'👍');assert.strictEqual(first.getAttribute('aria-pressed'),'true');ex(`toggleAIFlockReaction('${field}','👍')`);assert.strictEqual(q('#'+field).value,'');assert.strictEqual(first.getAttribute('aria-pressed'),'false');q('#'+field).value='🧠';ex(`syncAIFlockReactionPicker('${field}')`);assert(!box.children.some(button=>button.getAttribute('aria-pressed')==='true'))}
    ex("setRegistrationAIFlock('br','','🔥');waterAIJobs=[{id:301,name:'表情任务',groups:'-1001',start_time:'09:00',end_time:'10:00',prompt:'',reaction:'👏'}];openWaterAIJobForm(301)");assert.strictEqual(q('#brAiFlockReaction').value,'🔥');assert(q('#brAiFlockReactionPicker').children.some(button=>button.dataset.reactionEmoji==='🔥'&&button.getAttribute('aria-pressed')==='true'));assert.strictEqual(q('#waterAiJobReaction').value,'👏');assert(q('#waterAiJobReactionPicker').children.some(button=>button.dataset.reactionEmoji==='👏'&&button.getAttribute('aria-pressed')==='true'));
    ex("resetWaterAIJobForm();cancelAutoEdit('button_register');cancelAutoEdit('link_register');cancelAutoEdit('code_register')");for(const field of fields)assert.strictEqual(q('#'+field).value,'');assert(html.includes('.ai-flock-emojis button{min-width:44px!important;min-height:44px!important'));assert.strictEqual((html.match(/const EMOJIS=/g)||[]).length,1);for(const id of ['waterAiJobReactionPicker','brAiFlockReactionPicker','lkAiFlockReactionPicker','crAiFlockReactionPicker']){const fragment=html.slice(html.indexOf('id="'+id+'"'),html.indexOf('</div>',html.indexOf('id="'+id+'"')));assert(!fragment.includes('onclick='))}
  });
  await test('结构化detail及Pydantic数组提取人话，不输出input或对象',async()=>{
    for(const [detail,want] of [[{message:'当前账号任务不存在',ids:[42],input:'mock-secret'},'当前账号任务不存在'],[[{msg:'Field required',input:'mock-secret',loc:['body','name']}],'请补齐必填项'],[[{msg:'Input should be a valid integer',input:'mock-secret'}],'请填写有效的整数']]){
      app.fetch=async()=>({ok:false,status:422,json:async()=>({detail})});await assert.rejects(()=>realApi('/api/autos/batch'),e=>e.message.includes(want)&&!e.message.includes('mock-secret')&&!e.message.includes('[object Object]'));
    }
  });
  await test('旧账号同ID任务保存不写新账号，已发PUT不关新表单',async()=>{
    ex("selectedAccount='a';accountEpoch++;editAuto={id:42,type:'custom',enabled:1};editAutoAccount=accountContext()");q('#cName').value='任务A';q('#cTarget').value='@dummy';q('#cRetry').value='1';ex("setCustomTrigger('{\"mode\":\"manual\"}');setVisualSteps('[{\"t\":\"wait\",\"ms\":0}]')");let done,request;
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
  await test('共享图标库标题有统一SVG，整机迁移上传先预览后才能恢复',async()=>{
    const iconCard=html.slice(html.indexOf('>共享图标库<')-200,html.indexOf('>共享图标库<')+100);assert(iconCard.includes('<svg viewBox="0 0 24 24">'));
    for(const id of ['migrationCard','migrationPanelPassword','migrationBackupPassword','migrationBackupPasswordConfirm','migrationExport','migrationFile','migrationPreview','migrationApply','migrationResult'])assert(html.includes('id="'+id+'"'));
    q('#migrationPanelPassword').value='panel-password';q('#migrationBackupPassword').value='backup-password';q('#migrationFile').files=[{size:1024,name:'backup.tgp'}];let request;
    app.fetch=async(p,o)=>{request=[p,o];return {ok:true,status:200,json:async()=>({ok:true,preview:{id:'a'.repeat(32),accounts:2,files:5,bytes:2048,source_version:'1.16.1'}})}};
    await ex('previewMachineBackup()');assert.strictEqual(request[0],'/api/migration/import');assert.strictEqual(request[1].method,'PUT');assert.strictEqual(request[1].body.name,'backup.tgp');assert(!q('#migrationApply').disabled);assert(q('#migrationResult').textContent.includes('2 个账号'));
    app.window.confirm=()=>false;let applied=0;app.api=async()=>{applied++;return {}};await ex('applyMachineBackup()');assert.strictEqual(applied,0);
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
    assert(/id="signinGlobalFields" class="hidden"/.test(html));assert(html.includes('所有任务及失败重试轮完成后只推送一条汇总'));const button=q('global-toggle');app.globalButton=button;ex('toggleSigninGlobalFields(globalButton)');assert.strictEqual(button.getAttribute('aria-expanded'),'true');ex('toggleSigninGlobalFields(globalButton)');assert.strictEqual(button.getAttribute('aria-expanded'),'false');
  });
  await test('统一设置独立POST只保存三个keys，不连带AI或其他设置',async()=>{
    ex("selectedAccount='default';accountEpoch++;signinGlobalDirty=true;signinGlobalBusy=false");q('#signinGlobalEnabled').checked=true;q('#signinGlobalStart').value='00:05';q('#signinGlobalEnd').value='00:41';q('#aiProviderKey').value='mock-unsaved-key';q('#aiProviderBase').value='mock-unsaved-base';let requests=[];
    app.api=async(p,o)=>{requests.push([p,o]);return {ok:true}};app.loadAutos=async()=>{};await ex('saveSigninGlobal()');assert.strictEqual(requests.length,1);assert.strictEqual(requests[0][0],'/api/settings');assert.strictEqual(requests[0][1].method,'POST');assert.deepStrictEqual(JSON.parse(requests[0][1].body),{signin_global_enabled:'1',signin_global_start:'00:05',signin_global_end:'00:41'});assert.strictEqual(q('#aiProviderKey').value,'mock-unsaved-key');assert.strictEqual(q('#aiProviderBase').value,'mock-unsaved-base');assert.strictEqual(ex('signinGlobal.enabled'),true);assert(!q('#signinGlobalSave').disabled);
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
  await test('自定义任务卡摘要显示触发模式和准确步骤数',async()=>{
    for(const [mode,label] of [['manual','手动触发'],['schedule','定时触发'],['group','监听群触发']]){
      app.summaryTask={type:'custom',target:'@dummy',custom_trigger:JSON.stringify(mode==='schedule'?{mode,at:'2026-12-03T09:08:07'}:mode==='group'?{mode,groups:[],sender:'any',logic:'any',text:'注册',text_regex:false,button:'',button_regex:false,cooldown:0}:{mode}),custom_steps:JSON.stringify([{t:'wait',ms:0},{t:'send',to:'target',text:'/start',reply:false}]),chain_retry:1};
      const summary=ex('autoDesc(summaryTask)');assert(summary.includes(label));assert(summary.includes('2 步'));
      assert.strictEqual(ex('autoCanRun(summaryTask)'),mode==='manual');
    }
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
  await test('AI自定义草稿可读触发和六类步骤，填表与字段投影保持最新格式',async()=>{
    const trigger={mode:'group',groups:[],sender:'bot',logic:'any',text:'注册链接',text_regex:false,button:'注册',button_regex:false,cooldown:20};const steps=copy(ex("['send','wait','click','wait_reply','extract','captcha'].map(type=>STEP_HINTS[type])"));
    app.customDraft={type:'custom',name:'AI 注册流程',target:'{trigger.bot}',custom_trigger:JSON.stringify(trigger),custom_steps:JSON.stringify(steps),chain_retry:1,scheduled_at:'2026-12-03T09:08:07',enabled:0};app.customPreview={summary:'自定义草稿',draft:app.customDraft,elapsed_ms:5};
    const preview=ex('readableAIDraft(customPreview)');for(const text of ['监听群消息','发送消息','等待回复','提取内容','识别验证码'])assert(preview.includes(text));
    ex('applyDraftToForm(customDraft)');assert.deepStrictEqual(copy(ex('buildCustomTrigger()')),trigger);assert.deepStrictEqual(JSON.parse(q('#cSteps').value),steps);assert.strictEqual(q('#cTarget').value,'{trigger.bot}');
    const projected=copy(ex('taskConfig(customDraft)'));assert.strictEqual(projected.custom_trigger,app.customDraft.custom_trigger);assert.strictEqual(projected.custom_steps,app.customDraft.custom_steps);assert.strictEqual(projected.scheduled_at,app.customDraft.scheduled_at);
  });
  await test('AI含凭证占位符的自定义草稿必须先填表，不可直接保存',async()=>{
    app.credentialDraft={...app.customDraft,custom_steps:JSON.stringify([{t:'send',to:'{trigger.bot}',text:'{credential.user}',reply:false}])};ex('aiDraft={account:accountContext(),draft:credentialDraft};aiDraftBusy=false');let sent=0;app.api=async()=>{sent++;return {}};await ex('saveAIDraft()');assert.strictEqual(sent,0);assert(q('#toast').textContent.includes('用户名和安全码'));ex('resetAIAssistant()');
  });
  await test('AI填入表单不是编辑已有任务，保存仍默认停用且保留零次重试',async()=>{
    ex('aiDraft={account:accountContext(),draft:generated};aiDraftBusy=false');let sent=[];app.api=async(p,o)=>{sent.push([p,JSON.parse(o.body)]);return {ok:true}};
    ex('fillAIDraft()');assert.strictEqual(sent.length,0);assert.strictEqual(ex('editAuto'),null);
    assert.strictEqual(q('#sName').value,app.generated.name);assert.strictEqual(q('#sRetries').value,'0');
    assert(q('#af-signin').classList.contains('show'));await ex("saveAuto('signin')");
    assert.strictEqual(sent[0][0],'/api/autos');assert.strictEqual(sent[0][1].enabled,0);assert.strictEqual(sent[0][1].retries,0);
  });
  await test('AI直接保存二次确认，拒绝不提交、允许仅保存配置',async()=>{
    ex('aiDraft={account:accountContext(),draft:generated};aiDraftBusy=false');
    let sent=[];app.api=async(p,o)=>{sent.push([p,JSON.parse(o.body)]);return {ok:true}};
    app.window.confirm=()=>false;await ex('saveAIDraft()');assert.strictEqual(sent.length,0);
    app.window.confirm=()=>true;await ex('saveAIDraft()');assert.strictEqual(sent.length,1);assert.strictEqual(sent[0][1].enabled,0);assert.strictEqual(sent[0][1].signin_captcha,0);assert.strictEqual(sent[0][1].signin_recognition_mode,'none');
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
    app.loadTz=app.loadSessions=app.loadAIProviders=()=>{};ex("selectedAccount='settings-a';accountEpoch++");let resolves=[];app.api=()=>new Promise(r=>resolves.push(r));
    const old=ex('loadSettings()');ex("selectedAccount='settings-b';accountEpoch++");const fresh=ex('loadSettings()');
    resolves[1]({task_concurrency:'5'});await fresh;assert.strictEqual(q('#sTaskConcurrency').value,'5');
    resolves[0]({task_concurrency:'1'});await old;assert.strictEqual(q('#sTaskConcurrency').value,'5');
    q('#sTaskConcurrency').value='4';let posted;app.api=async(p,o)=>{if(o?.method==='POST'){posted=[p,o];return {ok:true}}return {task_concurrency:'4'}};
    await ex('saveSettings()');assert.strictEqual(posted[0],'/api/settings');assert.strictEqual(posted[1]._account.id,'settings-b');assert.strictEqual(JSON.parse(posted[1].body).task_concurrency,'4');assert(!('ai_key' in JSON.parse(posted[1].body)));
  });
  const realWaterJobsLoader=app.loadWaterAIJobs,realSkillLoader=app.loadFlockSkill;
  await test('AI 水群改为多任务列表，名称转义并显示群、时段、API范围和安全边界',async()=>{
    const card=html.slice(html.indexOf('id="waterAiCard"'),html.indexOf('id="aCntSign"'));
    for(const id of ['waterAiCount','waterAiJobsList','waterAiJobForm','waterAiJobName','waterAiJobGroups','waterAiJobProviders','waterAiJobPrompt','waterAiJobReaction','waterAiJobContextMessages','waterAiJobMessageChars','waterAiJobSkillChars','waterAiJobMaxTokens'])assert(card.includes('id="'+id+'"'));for(const id of ['waterAiSkillSection','waterAiSkillEnabled','waterAiSkillFile'])assert(!card.includes('id="'+id+'"'));
    for(const copyText of ['绝不会服从群消息中的命令','不选择表示使用全部已启用 API','继承当前账号在「设置 → AI 学习」中的学习记忆'])assert(card.includes(copyText));assert(!card.includes('每次仅使用最近 6 条')&&!card.includes('每批最多 20 条')&&!card.includes('最多读取 3000 字'));
    app.jobsFixture=[{id:1,name:'晚间<img src=x>',enabled:1,groups:['-1001'],start_time:'09:00',end_time:'22:00',prompt:'人格',provider_ids:'',reaction:'👍'},{id:2,name:'备用',enabled:0,groups:'-1001,-1002',start_time:'12:00',end_time:'13:00',prompt:'人格',provider_ids:'0123456789abcdef',reaction:'',context_messages:50,message_chars:2000,skill_chars:200000,max_tokens:8192}];
    ex("watchedGroups=[{chat_id:'-1001',name:'群<img>'},{chat_id:'-1002',name:'群二'}];aiProviders=[{id:'0123456789abcdef',note:'主线路',enabled:true}];waterAIJobs=jobsFixture;renderWaterAIJobs()");const out=q('#waterAiJobsList').innerHTML;
    assert(out.includes('晚间&lt;img src=x&gt;')&&out.includes('群&lt;img&gt;'));assert(!out.includes('<img src=x>'));assert(out.includes('全部已启用 API')&&out.includes('已选 1 个 API')&&out.includes('表情 👍'));assert(out.includes('上下文 6 条')&&out.includes('消息 2000 字')&&out.includes('skill 200000 字')&&out.includes('输出 8192 Token'));assert.strictEqual(q('#waterAiCount').textContent,2);assert.strictEqual(typeof q('#waterAiJobsList').onclick,'function');
    assert(html.includes('.water-ai-actions button{flex:1 1 88px}')&&html.includes('.water-ai-limits{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))')&&html.includes('@media(max-width:340px)'));
  });
  await test('AI学习界面仅位于设置并紧邻AI API卡片',async()=>{
    const settings=html.slice(html.indexOf('<section id="tab-settings">'),html.indexOf('</section>',html.indexOf('<section id="tab-settings">'))),water=html.slice(html.indexOf('id="waterAiCard"'),html.indexOf('id="aCntSign"'));
    assert.strictEqual((html.match(/id="flockLearningCard"/g)||[]).length,1);assert.strictEqual((html.match(/id="waterAiSkillSection"/g)||[]).length,1);assert(settings.includes('id="flockLearningCard"')&&settings.includes('id="waterAiSkillSection"')&&settings.includes('>AI 学习</span>'));assert(!water.includes('id="waterAiSkillSection"'));assert(settings.indexOf('id="foldAiProviders"')<settings.indexOf('id="flockLearningCard"'));assert(settings.indexOf('id="flockLearningCard"')<settings.indexOf('id="migrationCard"'));
  });
  await test('水群任务新增编辑重置及启停删除复制只调用独立CRUD接口',async()=>{
    ex("selectedAccount='flock-a';accountEpoch++;waterAIJobs=[jobsFixture[1]];openWaterAIJobForm(2)");assert.strictEqual(q('#waterAiJobName').value,'备用');assert.strictEqual(ex("[...waterAIJobGroupSel].join(',')"),'-1001,-1002');assert.strictEqual(ex("[...waterAIJobProviderSel].join(',')"),'0123456789abcdef');assert.deepStrictEqual(['ContextMessages','MessageChars','SkillChars','MaxTokens'].map(s=>q('#waterAiJob'+s).value),['50','2000','200000','8192']);
    q('#waterAiJobName').value='更新任务';q('#waterAiJobEnabled').checked=true;q('#waterAiJobStart').value='08:00';q('#waterAiJobEnd').value='09:00';q('#waterAiJobPrompt').value='更新人格';q('#waterAiJobReaction').value='🔥';q('#waterAiJobContextMessages').value='1';q('#waterAiJobMessageChars').value='40';q('#waterAiJobSkillChars').value='0';q('#waterAiJobMaxTokens').value='50';ex("waterAIJobGroupSel=new Set(['-1001']);waterAIJobProviderSel=new Set(['0123456789abcdef'])");const requests=[];app.api=async(p,o)=>{requests.push([p,o]);return {ok:true,id:3}};app.loadWaterAIJobs=async()=>{};
    await ex('saveWaterAIJob()');assert.strictEqual(requests[0][0],'/api/ai/flock/jobs/2');assert.strictEqual(requests[0][1].method,'PUT');assert.strictEqual(requests[0][1]._account.id,'flock-a');assert.deepStrictEqual(JSON.parse(requests[0][1].body),{name:'更新任务',enabled:1,groups:'-1001',start_time:'08:00',end_time:'09:00',prompt:'更新人格',provider_ids:'0123456789abcdef',reaction:'🔥',context_messages:1,message_chars:40,skill_chars:0,max_tokens:50,knowledge_chars:2000,knowledge_on_demand:0,end_next_day:0});assert(q('#waterAiJobForm').classList.contains('hidden'));
    ex('waterAIJobs=[jobsFixture[1]]');app.window.confirm=()=>true;for(const action of ['toggle','duplicate','delete'])await ex(`mutateWaterAIJob('2','${action}',accountContext())`);assert.deepStrictEqual(requests.slice(1).map(x=>[x[0],x[1].method]),[['/api/ai/flock/jobs/2','PUT'],['/api/ai/flock/jobs','POST'],['/api/ai/flock/jobs/2','DELETE']]);const duplicate=JSON.parse(requests[2][1].body);assert.strictEqual(duplicate.enabled,0);assert(duplicate.name.endsWith('副本'));assert.deepStrictEqual([duplicate.context_messages,duplicate.message_chars,duplicate.skill_chars,duplicate.max_tokens],[50,2000,200000,8192]);assert(!('id' in duplicate));
    ex('openWaterAIJobForm();resetWaterAIJobForm()');assert.strictEqual(ex('editingWaterAIJobId'),null);assert.strictEqual(ex('waterAIJobGroupSel.size'),0);assert.strictEqual(q('#waterAiJobPrompt').value,'');assert.deepStrictEqual(['ContextMessages','MessageChars','SkillChars','MaxTokens'].map(s=>q('#waterAiJob'+s).value),['6','120','3000','200']);app.window.confirm=()=>false;
  });
  await test('水群次日结束可保存回填复制，列表注明次日且拒绝超过24小时',async()=>{
    app.nightFixture={id:41,name:'夜间',enabled:1,groups:'-1001',start_time:'22:00',end_time:'02:00',end_next_day:1,prompt:'聊天',provider_ids:'0123456789abcdef'};
    ex("selectedAccount='overnight';accountEpoch++;watchedGroups=[{chat_id:'-1001',name:'群'}];aiProviders=[{id:'0123456789abcdef',enabled:true}];waterAIJobs=[nightFixture];openWaterAIJobForm(41);renderWaterAIJobs()");assert(q('#waterAiJobEndNextDay').checked);assert(q('#waterAiJobsList').innerHTML.includes('22:00–次日 02:00'));
    let requests=[];app.api=async(p,o)=>{requests.push([p,o]);return {ok:true}};app.loadWaterAIJobs=async()=>{};await ex('saveWaterAIJob()');assert.strictEqual(JSON.parse(requests[0][1].body).end_next_day,1);await ex("mutateWaterAIJob('41','duplicate')");assert.strictEqual(JSON.parse(requests[1][1].body).end_next_day,1);
    for(const [start,end,nextDay] of [['22:00','02:00',false],['10:00','11:00',true],['25:00','02:00',true]]){ex("openWaterAIJobForm();waterAIJobGroupSel=new Set(['-1001'])");q('#waterAiJobName').value='无效';q('#waterAiJobStart').value=start;q('#waterAiJobEnd').value=end;q('#waterAiJobEndNextDay').checked=nextDay;requests=[];await ex('saveWaterAIJob()');assert.strictEqual(requests.length,0);assert(q('#toast').textContent.includes('24 小时'))}
    ex('resetWaterAIJobForm()');assert(!q('#waterAiJobEndNextDay').checked);
  });
  await test('水群人格可留空纯skill模仿，启用但skill为空时显示后端错误',async()=>{
    const card=html.slice(html.indexOf('id="waterAiCard"'),html.indexOf('id="aCntSign"'));for(const text of ['人格提示词（可选）','留空时完全模仿账号 skill.md','填写后在主人风格上叠加该任务人格'])assert(card.includes(text));
    ex("selectedAccount='persona-empty';accountEpoch++;watchedGroups=[{chat_id:'-1001',name:'群'}];aiProviders=[{id:'0123456789abcdef',note:'线路',enabled:true}];waterAIJobs=[{id:18,name:'纯skill',enabled:1,groups:'-1001',start_time:'09:00',end_time:'10:00',prompt:'',provider_ids:'0123456789abcdef'}];openWaterAIJobForm(18)");assert.strictEqual(q('#waterAiJobPrompt').value,'');q('#waterAiJobEnabled').checked=true;let body;app.api=async(_p,o)=>{body=JSON.parse(o.body);throw new Error('启用纯 skill 模仿前请先生成或导入 skill.md')};app.loadWaterAIJobs=async()=>{};await ex('saveWaterAIJob()');assert.strictEqual(body.prompt,'');assert(q('#toast').textContent.includes('请先生成或导入 skill.md'));assert(!q('#waterAiJobForm').classList.contains('hidden'));ex('resetWaterAIJobForm()');assert.strictEqual(q('#waterAiJobPrompt').value,'');
  });
  await test('水群预算横幅随输入更新且鲸鱼学习不改变回复预算',async()=>{
    const card=html.slice(html.indexOf('id="waterAiCard"'),html.indexOf('id="aCntSign"'));for(const text of ['当前水群预算：任务预设 6条/120字/skill 3000字/200 Token','鲸鱼模式仅影响学习，不影响水群回复预算','学习中心：鲸鱼模式生效中，水群仍使用本任务预设'])assert(card.includes(text));ex('openWaterAIJobForm()');q('#waterAiJobContextMessages').value='9';q('#waterAiJobMessageChars').value='240';q('#waterAiJobSkillChars').value='7000';q('#waterAiJobMaxTokens').value='650';ex('renderWaterAIBudgetStatus()');assert.strictEqual(q('#waterAiJobBudgetText').textContent,'当前水群预算：任务预设 9条/240字/skill 7000字/650 Token；鲸鱼模式仅影响学习，不影响水群回复预算');ex("renderFlockSkill({flock_learning_unlimited_active:true})");assert(!q('#waterAiJobWhaleBadge').classList.contains('hidden'));ex("renderFlockSkill({flock_learning_unlimited_active:false})");assert(q('#waterAiJobWhaleBadge').classList.contains('hidden'));
  });
  await test('水群任务限制拒绝越界和非整数且不调用接口',async()=>{
    ex("selectedAccount='limits-job';accountEpoch++;watchedGroups=[{chat_id:'-1001',name:'群'}];aiProviders=[{id:'0123456789abcdef',enabled:true}]");let sent=0;app.api=async()=>{sent++;return {}};const invalid={ContextMessages:['0','51','1.5'],MessageChars:['39','2001','40.1'],SkillChars:['-1','200001','0.5'],MaxTokens:['49','8193','NaN']};
    invalid.SkillChars.push('');for(const [suffix,values] of Object.entries(invalid))for(const value of values){ex('openWaterAIJobForm()');q('#waterAiJobName').value='限制';q('#waterAiJobPrompt').value='人格';ex("waterAIJobGroupSel=new Set(['-1001'])");q('#waterAiJob'+suffix).value=value;await ex('saveWaterAIJob()');assert.strictEqual(sent,0);assert(q('#toast').textContent.includes('整数'));ex('resetWaterAIJobForm()')}
  });
  await test('多条API水群任务重新载入保留逗号分隔ID，不会误报全部失效',async()=>{
    app.loadWaterAIJobs=realWaterJobsLoader;
    ex("selectedAccount='multi-api';accountEpoch++;aiProviders=[{id:'1111111111111111',note:'一',enabled:true},{id:'2222222222222222',note:'二',enabled:true}];aiProvidersLoaded=true;watchedGroups=[{chat_id:'-1001',name:'群'}];watchedGroupsLoaded=true");
    app.api=async()=>({jobs:[{id:77,name:'多线路',enabled:1,groups:'-1001',start_time:'09:00',end_time:'10:00',prompt:'聊',provider_ids:'1111111111111111,2222222222222222'}]});
    await ex('loadWaterAIJobs()');assert.strictEqual(ex('waterAIJobs[0].provider_ids'),'1111111111111111,2222222222222222');
    ex('openWaterAIJobForm(77)');assert.strictEqual(ex('waterAIJobProviderExplicit'),true);assert.strictEqual(ex('[...waterAIJobProviderSel].join(",")'),'1111111111111111,2222222222222222');assert(!ex('rejectBrokenProviderSelection(waterAIJobProviderExplicit,waterAIJobProviderSel)'));
    let sent=0;app.api=async()=>{sent++;return {ok:true}};app.loadWaterAIJobs=async()=>{};q('#waterAiJobName').value='多线路';await ex("mutateWaterAIJob('77','toggle')");assert.strictEqual(sent,1);
    ex("waterAIJobs=[{id:78,name:'多线路',enabled:0,groups:'-1001',start_time:'09:00',end_time:'10:00',prompt:'聊',provider_ids:'1111111111111111,2222222222222222'}];openWaterAIJobForm(78)");let savedBody;app.api=async(_p,o)=>{savedBody=JSON.parse(o.body);return {ok:true}};await ex('saveWaterAIJob()');assert.strictEqual(savedBody.provider_ids,'1111111111111111,2222222222222222');
  });
  await test('水群任务加载和保存冻结账号，迟到列表及响应不能覆盖新账号表单',async()=>{
    app.loadWaterAIJobs=realWaterJobsLoader;ex("selectedAccount='old-flock';accountEpoch++;waterAIJobs=[]");let resolve;app.api=()=>new Promise(r=>resolve=r);const loading=ex('loadWaterAIJobs()');ex("selectedAccount='new-flock';accountEpoch++;waterAIJobs=[];waterAIJobsSeq++");resolve({jobs:[{id:9,name:'迟到任务'}]});await loading;assert.strictEqual(ex('waterAIJobs.length'),0);assert(!q('#waterAiJobsList').innerHTML.includes('迟到任务'));
    ex("selectedAccount='save-a';accountEpoch++;watchedGroups=[{chat_id:'-1001',name:'群'}];aiProviders=[{id:'0123456789abcdef',enabled:true}];openWaterAIJobForm();waterAIJobGroupSel=new Set(['-1001'])");q('#waterAiJobName').value='账号A';q('#waterAiJobStart').value='09:00';q('#waterAiJobEnd').value='10:00';q('#waterAiJobPrompt').value='人格';let done;app.api=()=>new Promise(r=>done=r);const saving=ex('saveWaterAIJob()');ex("selectedAccount='save-b';accountEpoch++");q('#waterAiJobName').value='账号B';done({ok:true,id:10});await saving;assert.strictEqual(q('#waterAiJobName').value,'账号B');assert.strictEqual(ex('editingWaterAIJobId'),null);
  });
  await test('水群保存与宽泛设置保存互不连带',async()=>{
    ex("selectedAccount='default';accountEpoch++;resetWaterAIState()");q('#sTaskConcurrency').value='2';const requests=[];app.api=async(p,o)=>{requests.push([p,o]);return {ok:true}};app.loadSettings=()=>{};await ex('saveSettings()');const broad=JSON.parse(requests[0][1].body);assert.strictEqual(requests[0][0],'/api/settings');for(const field of ['water_ai_enabled','water_ai_groups','water_ai_prompt','name','provider_ids','reaction'])assert(!Object.prototype.hasOwnProperty.call(broad,field));
    ex("watchedGroups=[{chat_id:'-1001',name:'群'}];aiProviders=[{id:'0123456789abcdef',enabled:true}];openWaterAIJobForm();waterAIJobGroupSel=new Set(['-1001'])");q('#waterAiJobName').value='独立保存';q('#waterAiJobStart').value='09:00';q('#waterAiJobEnd').value='10:00';q('#waterAiJobPrompt').value='人格';app.loadWaterAIJobs=async()=>{};await ex('saveWaterAIJob()');assert.strictEqual(requests[1][0],'/api/ai/flock/jobs');assert(!requests.slice(1).some(x=>x[0]==='/api/settings'));
  });
  await test('注册成功水群三表单各自维护API选择与表情，空选择语义明确且停用API拒绝保存',async()=>{
    for(const prefix of ['br','lk','cr'])for(const suffix of ['AiFlockProviders','AiFlockReaction'])assert(html.includes('id="'+prefix+suffix+'"'));assert((html.match(/不选择表示使用全部已启用 API；多选后按所选列表轮询。/g)||[]).length>=4);
    ex("aiProviders=[{id:'0123456789abcdef',note:'启用',enabled:true},{id:'aaaaaaaaaaaaaaaa',note:'停用',enabled:false}];setRegistrationAIFlock('br','0123456789abcdef','👍');setRegistrationAIFlock('lk','','');setRegistrationAIFlock('cr','aaaaaaaaaaaaaaaa','🔥')");assert.strictEqual(ex('registrationAIProviderSel.br.size'),1);assert.strictEqual(ex('registrationAIProviderSel.lk.size'),0);assert.strictEqual(q('#crAiFlockReaction').value,'🔥');assert(q('#crAiFlockProviders').innerHTML.includes('已停用：停用'));assert(q('#crAiFlockProviders').innerHTML.includes('repair'));
    ex("cancelAutoEdit('link_register')");assert.strictEqual(ex('registrationAIProviderSel.lk.size'),0);assert.strictEqual(ex('registrationAIProviderSel.br.size'),1);assert.strictEqual(ex('registrationAIProviderSel.cr.size'),1);assert(!ex("selectedAIProvidersEnabled('aaaaaaaaaaaaaaaa')"));assert(ex("selectedAIProvidersEnabled('')"));
  });
  await test('任务学习注册编辑器显示可移除的停用失效API修复项',async()=>{
    app.providerRepair=[{id:'1111111111111111',note:'正常线路',enabled:true},{id:'2222222222222222',note:'停用线路',enabled:false}];ex("aiProviders=providerRepair;aiProvidersLoaded=true;watchedGroups=[{chat_id:'-1001',name:'有效群'}];watchedGroupsLoaded=true;staleReferenceNoticeShown=false;waterAIJobProviderSel=new Set(['2222222222222222','3333333333333333']);waterAISkillProviderSel=new Set(['2222222222222222','3333333333333333']);registrationAIProviderSel.br=new Set(['2222222222222222','3333333333333333']);waterAIJobGroupSel=new Set(['-1001','-9999']);waterAISkillGroupSel=new Set(['-9999']);renderWaterAIProviderPicks();renderRegistrationAIProviders('br');renderWaterAIGroups()");
    const job=q('#waterAiJobProviders').innerHTML,learning=q('#waterAiSkillProviders').innerHTML,registration=q('#brAiFlockProviders').innerHTML;for(const markup of [job,learning,registration])assert(markup.includes('已停用：停用线路')&&markup.includes('repair')&&markup.includes('aria-invalid="true"'));assert(!job.includes('3333333333333333')&&!learning.includes('3333333333333333')&&!registration.includes('3333333333333333'));assert.strictEqual(ex('waterAIJobProviderSel.size'),1);assert.strictEqual(ex('waterAISkillProviderSel.size'),1);assert.strictEqual(ex('registrationAIProviderSel.br.size'),1);assert.deepStrictEqual(copy(ex('[...waterAIJobGroupSel]')),['-1001']);assert.strictEqual(ex('waterAISkillGroupSel.size'),0);assert(q('#toast').textContent.includes('已移除当前账号中不存在的旧群或 AI API 引用'));
    ex("toggleProviderSelection(waterAIJobProviderSel,'2222222222222222',renderWaterAIProviderPicks)");assert.strictEqual(ex('waterAIJobProviderSel.size'),0,'停用任务 API 应可移除');ex("toggleProviderSelection(waterAISkillProviderSel,'2222222222222222',renderWaterAIProviderPicks)");assert.strictEqual(ex('waterAISkillProviderSel.size'),0,'停用学习 API 应可移除');ex("toggleProviderSelection(registrationAIProviderSel.br,'2222222222222222',()=>renderRegistrationAIProviders('br'))");assert.strictEqual(ex('registrationAIProviderSel.br.size'),0,'停用注册 API 应可移除');assert.strictEqual(ex("waterAIJobPayload({provider_ids:[...waterAIJobProviderSel],groups:[...waterAIJobGroupSel]}).provider_ids"),'');
    ex("cancelAutoEdit('button_register')");q('#brName').value='修复引用';q('#brUser').value='user';q('#brSecret').value='secret';const requests=[];app.api=async(p,o)=>{requests.push([p,o]);return {id:201}};app.loadAutos=async()=>{};await ex("saveAuto('button_register')");assert.strictEqual(JSON.parse(requests[0][1].body).ai_flock_provider_ids,'');
    ex('resetFlockSkillForm()');app.api=async(p,o)=>{requests.push([p,o]);return {ok:true}};app.loadFlockSkill=async()=>{};await ex('saveFlockSkill()');assert.strictEqual(JSON.parse(requests.at(-1)[1].body).flock_learning_provider_ids,'');
  });
  await test('保存时API或群列表尚未加载仍保留原选择，不误报失效',async()=>{
    ex("selectedAccount='pending-load';accountEpoch++;aiProvidersLoaded=false;watchedGroupsLoaded=false;waterAIJobs=[];aiProviders=[];watchedGroups=[];openWaterAIJobForm();waterAIJobGroupSel=new Set(['-1001']);aiProviders=[{id:'1111111111111111',enabled:true}]");q('#waterAiJobName').value='等待加载';q('#waterAiJobStart').value='09:00';q('#waterAiJobEnd').value='10:00';q('#waterAiJobPrompt').value='聊';
    let sent=0;app.api=async()=>{sent++;return {ok:true}};app.loadWaterAIJobs=async()=>{};
    ex("waterAIJobGroupSel=new Set(['-1001']);waterAIJobProviderSel=new Set(['1111111111111111'])");
    await ex('saveWaterAIJob()');assert.strictEqual(sent,1);assert(q('#toast').textContent!=='原 API 已失效，请重新选择至少一个已启用 API');
    ex('resetWaterAIJobForm()');
  });
  await test('列表未加载时保存不清空已选API或群，加载后才过滤失效项',async()=>{
    ex("selectedAccount='guard';accountEpoch++;aiProvidersLoaded=false;watchedGroupsLoaded=false;aiProviders=[];watchedGroups=[]");
    assert.strictEqual(ex("enabledProviderCsv('1111111111111111,2222222222222222')"),'1111111111111111,2222222222222222');
    assert.strictEqual(ex("watchedGroupCsv('-1001,-1002')"),'-1001,-1002');
    assert.strictEqual(ex("explicitProviderSelectionBroken(true,'1111111111111111')"),false);
    assert.strictEqual(ex("selectedAIProvidersEnabled('1111111111111111')"),true);
    ex("aiProvidersLoaded=true;aiProviders=[{id:'1111111111111111',enabled:true}]");assert.strictEqual(ex("enabledProviderCsv('1111111111111111,2222222222222222')"),'1111111111111111');assert.strictEqual(ex("explicitProviderSelectionBroken(true,'2222222222222222')"),true);
    ex("watchedGroupsLoaded=true;watchedGroups=[{chat_id:'-1001'}]");assert.strictEqual(ex("watchedGroupCsv('-1001,-1002')"),'-1001');
  });
  await test('显式API全失效阻止水群注册学习保存，混合引用只保存有效项',async()=>{
    app.validProviders=[{id:'1111111111111111',note:'有效',enabled:true}];ex("selectedAccount='explicit-provider';accountEpoch++;aiProviders=validProviders;aiProvidersLoaded=true;watchedGroups=[{chat_id:'-1001',name:'群'}];watchedGroupsLoaded=true;staleReferenceNoticeShown=false;waterAIJobs=[{id:401,name:'全失效',enabled:0,groups:'-1001',start_time:'09:00',end_time:'10:00',prompt:'',provider_ids:'3333333333333333'}];openWaterAIJobForm(401)");assert(ex('waterAIJobProviderExplicit'));assert.strictEqual(ex('waterAIJobProviderSel.size'),0);let requests=[];app.api=async(p,o)=>{requests.push([p,o]);return {ok:true}};app.loadWaterAIJobs=async()=>{};await ex('saveWaterAIJob()');assert.strictEqual(requests.length,0);assert.strictEqual(q('#toast').textContent,'原 API 已失效，请重新选择至少一个已启用 API');
    ex("waterAIJobs=[{id:402,name:'混合',enabled:0,groups:'-1001',start_time:'09:00',end_time:'10:00',prompt:'',provider_ids:'3333333333333333,1111111111111111'}];openWaterAIJobForm(402)");await ex('saveWaterAIJob()');assert.strictEqual(JSON.parse(requests.at(-1)[1].body).provider_ids,'1111111111111111');ex("openWaterAIJobForm();waterAIJobGroupSel=new Set(['-1001'])");q('#waterAiJobName').value='全部线路';q('#waterAiJobStart').value='10:00';q('#waterAiJobEnd').value='11:00';await ex('saveWaterAIJob()');assert.strictEqual(JSON.parse(requests.at(-1)[1].body).provider_ids,'');assert.strictEqual(ex('waterAIJobProviderExplicit'),false);
    let sent=0;app.api=async()=>{sent++;return {id:500}};for(const [type,prefix] of [['button_register','br'],['link_register','lk'],['code_register','cr']]){ex("cancelAutoEdit('"+type+"');setRegistrationAIFlock('"+prefix+"','3333333333333333','')");assert(ex('registrationAIProviderExplicit.'+prefix));await ex("saveAuto('"+type+"')");assert.strictEqual(sent,0);assert.strictEqual(q('#toast').textContent,'原 API 已失效，请重新选择至少一个已启用 API')}
    ex("cancelAutoEdit('link_register');setRegistrationAIFlock('lk','3333333333333333,1111111111111111','')");q('#lkName').value='混合注册';q('#lkButton').value='注册';q('#lkReady').value='准备';q('#lkSuccess').value='成功';q('#lkUser').value='user';q('#lkSecret').value='secret';requests=[];app.api=async(p,o)=>{requests.push([p,o]);return {id:501}};app.loadAutos=async()=>{};await ex("saveAuto('link_register')");assert.strictEqual(JSON.parse(requests[0][1].body).ai_flock_provider_ids,'1111111111111111');ex("cancelAutoEdit('button_register')");q('#brName').value='全部注册';q('#brUser').value='user';q('#brSecret').value='secret';requests=[];await ex("saveAuto('button_register')");assert.strictEqual(JSON.parse(requests[0][1].body).ai_flock_provider_ids,'');assert.strictEqual(ex('registrationAIProviderExplicit.br'),false);
    app.loadFlockSkill=realSkillLoader;app.api=async()=>({flock_learning_provider_ids:'3333333333333333'});await ex('loadFlockSkill()');assert(ex('waterAISkillProviderExplicit'));assert.strictEqual(ex('waterAISkillProviderSel.size'),0);sent=0;app.api=async()=>{sent++;return {ok:true}};await ex('saveFlockSkill()');assert.strictEqual(sent,0);assert.strictEqual(q('#toast').textContent,'原 API 已失效，请重新选择至少一个已启用 API');app.api=async()=>({flock_learning_provider_ids:'3333333333333333,1111111111111111'});app.loadFlockSkill=realSkillLoader;await ex('loadFlockSkill()');requests=[];app.api=async(p,o)=>{requests.push([p,o]);return {ok:true}};app.loadFlockSkill=async()=>{};await ex('saveFlockSkill()');assert.strictEqual(JSON.parse(requests[0][1].body).flock_learning_provider_ids,'1111111111111111');ex('resetFlockSkillForm()');requests=[];await ex('saveFlockSkill()');assert.strictEqual(JSON.parse(requests[0][1].body).flock_learning_provider_ids,'');assert.strictEqual(ex('waterAISkillProviderExplicit'),false);
  });
  await test('学习配置载入统计并精确独立保存，立即学习和上传使用专用接口',async()=>{
    app.loadFlockSkill=realSkillLoader;ex("selectedAccount='skill-a';accountEpoch++;watchedGroups=[{chat_id:'-1001',name:'学习群'}];aiProviders=[{id:'0123456789abcdef',note:'学习线路',enabled:true}]");app.api=async p=>{assert.strictEqual(p,'/api/ai/flock/skill');return {flock_learning_enabled:'1',flock_learning_start:'01:00',flock_learning_end:'02:00',flock_learning_groups:'-1001',flock_learning_provider_ids:'0123456789abcdef',flock_learning_unlimited:'1',flock_learning_unlimited_start:'2026-11-12T10:00',flock_learning_unlimited_end:'2026-11-12T12:00',flock_deep_learning_enabled:'1',flock_deep_learning_start:'2026-11-13T08:00',flock_deep_learning_end:'2026-11-13T10:00',flock_skill_upload_enabled:'1',flock_skill_upload_time:'02:10',flock_skill_distill_enabled:'1',flock_skill_distill_time:'04:30',flock_learning_batch_size:33,flock_learning_message_chars:240,flock_learning_skill_chars:9000,flock_learning_max_tokens:777,sample_count:7,skill_size:1536,flock_skill_last_upload_date:'2026-11-12',recent_block_count:9,detail_block_count:8,summary_block_count:3,fading_block_count:2,flock_learning_unlimited_active:true,flock_deep_learning_active:false}};await ex('loadFlockSkill()');assert(q('#waterAiSkillEnabled').checked);assert.strictEqual(q('#waterAiSkillStart').value,'01:00');assert.strictEqual(ex("[...waterAISkillGroupSel].join(',')"),'-1001');assert(q('#waterAiSkillUnlimited').checked);assert.strictEqual(q('#waterAiSkillUnlimitedStart').value,'2026-11-12T10:00');assert.strictEqual(q('#waterAiSkillUnlimitedEnd').value,'2026-11-12T12:00');assert(q('#waterAiSkillDeepEnabled').checked);assert.strictEqual(q('#waterAiSkillDeepStart').value,'2026-11-13T08:00');assert.strictEqual(q('#waterAiSkillDeepEnd').value,'2026-11-13T10:00');assert(['BatchSize','MessageChars','SkillChars','MaxTokens'].every(s=>q('#waterAiSkill'+s).disabled));assert(q('#waterAiSkillDistillEnabled').checked);assert.strictEqual(q('#waterAiSkillDistillTime').value,'04:30');assert(!q('#waterAiSkillDistillTime').disabled&&!q('#waterAiSkillUploadTime').disabled&&!q('#waterAiSkillDeepStart').disabled);assert(q('#waterAiSkillActiveStatus').textContent.includes('鲸鱼模式'));assert.deepStrictEqual(['BatchSize','MessageChars','SkillChars','MaxTokens'].map(s=>q('#waterAiSkill'+s).value),['33','240','9000','777']);assert(q('#waterAiSkillSamples').textContent.includes('7'));assert(q('#waterAiSkillSize').textContent.includes('1.5 KiB'));assert(q('#waterAiSkillLastUpload').textContent.includes('2026-11-12'));assert(!q('#waterAiSkillGeneratedStats').classList.contains('hidden'));assert.strictEqual(q('#waterAiSkillRecentCount').textContent,'近期记忆：9');assert.strictEqual(q('#waterAiSkillDetailCount').textContent,'详细记忆：8');assert.strictEqual(q('#waterAiSkillSummaryCount').textContent,'摘要记忆：3');assert.strictEqual(q('#waterAiSkillFadingCount').textContent,'淡化记忆：2');
    const requests=[];app.api=async(p,o)=>{requests.push([p,o]);return p.endsWith('learn-now')?{learned:true}:p.endsWith('distill-now')?{distilled:true}:{ok:true}};app.loadFlockSkill=async()=>{};q('#waterAiSkillEnabled').checked=true;q('#waterAiSkillStart').value='03:00';q('#waterAiSkillEnd').value='04:00';q('#waterAiSkillUnlimitedStart').value='2026-12-01T09:30';q('#waterAiSkillUnlimitedEnd').value='2026-12-01T11:30';q('#waterAiSkillDeepEnabled').checked=true;q('#waterAiSkillDeepStart').value='2026-12-02T08:15';q('#waterAiSkillDeepEnd').value='2026-12-02T12:45';q('#waterAiSkillUploadEnabled').checked=true;q('#waterAiSkillUploadTime').value='04:10';q('#waterAiSkillDistillEnabled').checked=true;q('#waterAiSkillDistillTime').value='05:20';q('#waterAiSkillBatchSize').value='200';q('#waterAiSkillMessageChars').value='4096';q('#waterAiSkillSkillChars').value='5242880';q('#waterAiSkillMaxTokens').value='32768';ex("waterAISkillGroupSel=new Set(['-1001']);waterAISkillProviderSel=new Set(['0123456789abcdef'])");await ex('saveFlockSkill()');assert.strictEqual(requests[0][0],'/api/ai/flock/skill');assert.strictEqual(requests[0][1].method,'PUT');assert.deepStrictEqual(JSON.parse(requests[0][1].body),{flock_learning_enabled:1,flock_learning_start:'03:00',flock_learning_end:'04:00',flock_learning_groups:'-1001',flock_learning_provider_ids:'0123456789abcdef',flock_learning_unlimited:1,flock_learning_unlimited_start:'2026-12-01T09:30',flock_learning_unlimited_end:'2026-12-01T11:30',flock_deep_learning_enabled:1,flock_deep_learning_start:'2026-12-02T08:15',flock_deep_learning_end:'2026-12-02T12:45',flock_skill_upload_enabled:1,flock_skill_upload_time:'04:10',flock_skill_distill_enabled:1,flock_skill_distill_time:'05:20',flock_learning_batch_size:200,flock_learning_message_chars:4096,flock_learning_skill_chars:5242880,flock_learning_max_tokens:32768});
    await ex("runFlockSkillAction('learn-now')");await ex("runFlockSkillAction('distill-now')");await ex("runFlockSkillAction('upload-now')");assert.deepStrictEqual(requests.slice(1).map(x=>[x[0],x[1].method]),[['/api/ai/flock/skill/learn-now','POST'],['/api/ai/flock/skill/distill-now','POST'],['/api/ai/flock/skill/upload-now','POST']]);
    ex('renderFlockSkill({study_size:768})');assert.strictEqual(q('#waterAiStudySize').textContent,'study.md：768 B');
  });
  await test('学习状态面板位于保存上方并安全渲染全部后端状态与元数据',async()=>{
    const card=html.slice(html.indexOf('id="flockLearningCard"'),html.indexOf('id="migrationCard"')),statusAt=card.indexOf('id="waterAiSkillActiveStatus"'),saveAt=card.indexOf('id="waterAiSkillSave"'),statsAt=card.indexOf('id="waterAiSkillSamples"');assert(statusAt>0&&statusAt<saveAt&&saveAt<statsAt);for(const id of ['waterAiSkillStateTitle','waterAiSkillStateMeta','waterAiSkillStateError'])assert(card.includes('id="'+id+'"'));assert(card.includes('role="status" aria-live="polite"'));assert(!html.includes('.learning-status-dot{animation'));
    const cases={disabled:'未启用',waiting_window:'等待时段',collecting:'正在采集 7/10',ready:'已满足条件等待自动学习',waiting_automatic:'已满足条件等待自动学习',queued:'已排队等待学习',learning:'正在学习',success:'最近学习成功',whale:'土豪模式',deep:'深度学习',expired:'高成本学习已到期',capacity_stopped:'容量不足停止',error:'学习失败'};for(const [state,label] of Object.entries(cases)){app.statusFixture={learning_state:state,sample_count:7,learning_threshold:10};ex('renderFlockLearningStatus(statusFixture)');assert.strictEqual(q('#waterAiSkillStateTitle').textContent,label)}
    app.statusFixture={learning_state:'error',sample_count:9,learning_threshold:10,ai_learning_next_at:'2026-12-01 10:00',flock_learning_last_run_at:'123',flock_learning_last_success_at:'120',flock_learning_last_error:'<img src=x onerror=bad()>'};ex('renderFlockLearningStatus(statusFixture)');assert(q('#waterAiSkillStateMeta').textContent.includes('下次检查：2026-12-01 10:00'));assert(q('#waterAiSkillStateMeta').textContent.includes('最近运行：123'));assert(q('#waterAiSkillStateMeta').textContent.includes('最近成功：120'));assert.strictEqual(q('#waterAiSkillStateError').textContent,'错误：<img src=x onerror=bad()>');assert.strictEqual(q('#waterAiSkillStateError').innerHTML,'');assert(q('#waterAiSkillActiveStatus').className.includes('state-error'));
  });
  await test('立即学习先显示学习中，queued响应保留排队状态并等待轮询',async()=>{
    ex("selectedAccount='learn-queue';accountEpoch++;waterAISkillBusy=false");let done,loads=0;app.api=()=>new Promise(r=>done=r);app.loadFlockSkill=async()=>{loads++};const pending=ex("runFlockSkillAction('learn-now')");assert.strictEqual(q('#waterAiSkillStateTitle').textContent,'正在学习');assert(q('#waterAiLearnNow').disabled);done({ok:true,queued:true});await pending;assert.strictEqual(q('#waterAiSkillStateTitle').textContent,'已排队等待学习');assert(q('#waterAiSkillStatus').textContent.includes('已排队'));assert.strictEqual(loads,0);assert(!q('#waterAiLearnNow').disabled);
  });
  await test('学习GET防重叠且错误同时留在状态面板和操作提示',async()=>{
    app.loadFlockSkill=realSkillLoader;ex("selectedAccount='learn-load';accountEpoch++;waterAISkillLoading=false");q('#waterAiSkillBatchSize').value='123';let done,sent=0;app.api=()=>{sent++;return new Promise(r=>done=r)};const first=ex('loadFlockSkill(true)'),second=ex('loadFlockSkill(true)');assert.strictEqual(sent,1);done({learning_state:'collecting',sample_count:3,learning_threshold:10,flock_learning_batch_size:20});await Promise.all([first,second]);assert.strictEqual(q('#waterAiSkillStateTitle').textContent,'正在采集 3/10');assert.strictEqual(q('#waterAiSkillBatchSize').value,'123');assert.strictEqual(ex('waterAISkillLoading'),false);
    app.api=async()=>{throw new Error('<script>safe status error</script>')};await ex('loadFlockSkill()');assert.strictEqual(q('#waterAiSkillStateError').textContent,'错误：<script>safe status error</script>');assert(q('#waterAiSkillStatus').textContent.includes('safe status error'));assert.strictEqual(q('#waterAiSkillStateError').innerHTML,'');
  });
  await test('学习验收安全显示五项答案并处理忙碌错误和账号竞态',async()=>{
    const card=html.slice(html.indexOf('id="flockLearningCard"'),html.indexOf('id="migrationCard"'));assert(card.includes('id="waterAiSkillExam"')&&card.includes('id="waterAiSkillExamResult"')&&card.includes('<pre class="learning-exam-result"'));ex("selectedAccount='exam-a';accountEpoch++;waterAISkillBusy=false");let done,sent=0;app.api=(p,o)=>{sent++;assert.strictEqual(p,'/api/ai/flock/skill/exam');assert.strictEqual(o.method,'POST');return new Promise(r=>done=r)};const pending=ex('runFlockSkillExam()');const duplicate=ex('runFlockSkillExam()');assert.strictEqual(sent,1);assert(q('#waterAiSkillExam').disabled);done({answer:'1. 第一项\n2. <img src=x>\n3. 第三项\n4. 第四项\n5. 第五项'});await pending;await duplicate;assert(q('#waterAiSkillExamResult').textContent.includes('<img src=x>'));assert.strictEqual(q('#waterAiSkillExamResult').innerHTML,'');assert(q('#waterAiSkillStatus').textContent.includes('未修改 skill.md'));assert(!q('#waterAiSkillExam').disabled);
    let staleDone;app.api=()=>new Promise(r=>staleDone=r);const stale=ex('runFlockSkillExam()');ex("selectedAccount='exam-b';accountEpoch++;resetWaterAIState()");q('#waterAiSkillExamResult').textContent='新账号验收';staleDone({answer:'旧账号答案'});await stale;assert.strictEqual(q('#waterAiSkillExamResult').textContent,'新账号验收');app.api=async()=>{throw new Error('mock exam error')};await ex('runFlockSkillExam()');assert(q('#waterAiSkillExamResult').textContent.includes('验收失败：mock exam error'));assert(!q('#waterAiSkillExam').disabled);
  });
  await test('学习限制控件边界和重置默认准确，非法整数保存前拒绝',async()=>{
    const specs=[['BatchSize','20','10','200'],['MessageChars','160','40','4096'],['SkillChars','6000','500','5242880'],['MaxTokens','600','100','32768']];for(const [suffix,def,min,max] of specs){const input=html.match(new RegExp('<input\\b[^>]*id="waterAiSkill'+suffix+'"[^>]*>'))[0];for(const attr of ['value="'+def+'"','min="'+min+'"','max="'+max+'"','step="1"'])assert(input.includes(attr))}
    ex('resetFlockSkillForm()');assert(!q('#waterAiSkillUnlimited').checked&&!q('#waterAiSkillDeepEnabled').checked&&!q('#waterAiSkillDistillEnabled').checked);assert.strictEqual(q('#waterAiSkillUnlimitedStart').value,'');assert.strictEqual(q('#waterAiSkillUnlimitedEnd').value,'');assert.strictEqual(q('#waterAiSkillDeepStart').value,'');assert.strictEqual(q('#waterAiSkillDeepEnd').value,'');assert.strictEqual(q('#waterAiSkillDistillTime').value,'04:00');assert(specs.every(([suffix])=>!q('#waterAiSkill'+suffix).disabled));assert.deepStrictEqual(specs.map(([suffix])=>q('#waterAiSkill'+suffix).value),specs.map(x=>x[1]));let sent=0;app.api=async()=>{sent++;return {}};const invalid={BatchSize:['9','201','10.5',''],MessageChars:['39','4097','x'],SkillChars:['499','5242881','500.1'],MaxTokens:['99','32769','100.5']};for(const [suffix,values] of Object.entries(invalid))for(const value of values){q('#waterAiSkill'+suffix).value=value;await ex('saveFlockSkill()');assert.strictEqual(sent,0);assert(q('#toast').textContent.includes('整数'));ex('resetFlockSkillForm()')}
  });
  await test('无限学习默认关闭，警告完整并仅禁用预算输入且保留数值',async()=>{
    const card=html.slice(html.indexOf('id="flockLearningCard"'),html.indexOf('id="migrationCard"'));for(const text of ['高额 Token 费用','全部最多 200 条完整消息','完整 5 MiB skill.md','请求输出上限 32768 Token','实际仍受服务商与模型限制','到期后自动回到普通预算'])assert(card.includes(text));assert(card.includes('class="check water-ai-cost-toggle"')&&card.includes('aria-describedby="waterAiSkillUnlimitedWarning"'));assert(html.includes('#flockLearningCard label.check{min-height:44px}'));
    ex('resetFlockSkillForm()');const suffixes=['BatchSize','MessageChars','SkillChars','MaxTokens'],before=suffixes.map(s=>q('#waterAiSkill'+s).value);q('#waterAiSkillUnlimited').checked=true;ex('syncFlockLearningUnlimited()');assert(suffixes.every(s=>q('#waterAiSkill'+s).disabled));assert.deepStrictEqual(suffixes.map(s=>q('#waterAiSkill'+s).value),before);q('#waterAiSkillUnlimited').checked=false;ex('syncFlockLearningUnlimited()');assert(suffixes.every(s=>!q('#waterAiSkill'+s).disabled));assert.deepStrictEqual(suffixes.map(s=>q('#waterAiSkill'+s).value),before);
    ex("renderFlockSkill({flock_learning_unlimited:'0'})");assert(!q('#waterAiSkillUnlimited').checked);assert(suffixes.every(s=>!q('#waterAiSkill'+s).disabled));
  });
  await test('鲸鱼和深度学习北京时间窗口严格校验，深度模式不禁用普通预算',async()=>{
    const card=html.slice(html.indexOf('id="flockLearningCard"'),html.indexOf('id="migrationCard"'));for(const id of ['waterAiSkillUnlimitedStart','waterAiSkillUnlimitedEnd','waterAiSkillDeepEnabled','waterAiSkillDeepStart','waterAiSkillDeepEnd'])assert(card.includes('id="'+id+'"'));assert.strictEqual((card.match(/type="datetime-local"/g)||[]).length,4);assert((card.match(/北京时间/g)||[]).length>=8);for(const text of ['近期最多 200 条主人与群友消息','Token 消耗较高','到期自动关闭','上下文过长时会自动拆分','主人风格写入 skill.md','群内知识、话题与氛围写入 study.md'])assert(card.includes(text));
    ex("resetFlockSkillForm();watchedGroups=[{chat_id:'-1001',name:'群'}];aiProviders=[{id:'0123456789abcdef',enabled:true}];waterAISkillGroupSel=new Set(['-1001'])");const suffixes=['BatchSize','MessageChars','SkillChars','MaxTokens'];q('#waterAiSkillDeepEnabled').checked=true;q('#waterAiSkillDeepStart').value='2026-12-01T10:00';q('#waterAiSkillDeepEnd').value='2026-12-01T11:00';assert(suffixes.every(s=>!q('#waterAiSkill'+s).disabled));let sent=0;app.api=async()=>{sent++;return {ok:true}};app.loadFlockSkill=async()=>{};
    for(const [prefix,start,end] of [['Unlimited','2026-12-01T10:00:30','2026-12-01T11:00'],['Unlimited','2026-02-30T10:00','2026-12-01T11:00'],['Deep','2026-12-01T11:00','2026-12-01T11:00'],['Deep','2026-12-01T12:00','2026-12-01T11:00']]){ex('resetFlockSkillForm()');ex("waterAISkillGroupSel=new Set(['-1001'])");q(prefix==='Unlimited'?'#waterAiSkillUnlimited':'#waterAiSkillDeepEnabled').checked=true;q('#waterAiSkill'+prefix+'Start').value=start;q('#waterAiSkill'+prefix+'End').value=end;await ex('saveFlockSkill()');assert.strictEqual(sent,0);assert(q('#toast').textContent.includes('开始早于结束')||q('#toast').textContent.includes('精确到分钟'))}
  });
  await test('学习模式旧账号GET迟到时不覆盖新账号状态和时间窗',async()=>{
    app.loadFlockSkill=realSkillLoader;ex("selectedAccount='learn-old';accountEpoch++;resetWaterAIState()");let done;app.api=()=>new Promise(r=>done=r);const pending=ex('loadFlockSkill()');ex("selectedAccount='learn-new';accountEpoch++;resetWaterAIState()");q('#waterAiSkillStateTitle').textContent='新账号';done({flock_learning_unlimited:'1',flock_learning_unlimited_start:'2026-12-01T10:00',flock_learning_unlimited_end:'2026-12-01T12:00',flock_learning_unlimited_active:true});await pending;assert.strictEqual(q('#waterAiSkillStateTitle').textContent,'新账号');assert(!q('#waterAiSkillUnlimited').checked);assert.strictEqual(q('#waterAiSkillUnlimitedStart').value,'');
  });
  await test('提炼说明、统计、错误和账号迟到响应安全',async()=>{
    const card=html.slice(html.indexOf('id="flockLearningCard"'),html.indexOf('id="migrationCard"'));for(const text of ['保留 1 天完整内容','超过 1 天转为详细记忆','超过 7 天转为摘要','超过 60 天且至少经过 3 次提炼后逐渐淡化','每天按所选北京时间提炼','手动导入或固定的 Markdown 永远不会被自动删除'])assert(card.includes(text));for(const id of ['waterAiSkillDistillEnabled','waterAiSkillDistillTime','waterAiDistillNow','waterAiSkillGeneratedStats','waterAiSkillRecentCount','waterAiSkillDetailCount','waterAiSkillSummaryCount','waterAiSkillFadingCount'])assert(card.includes('id="'+id+'"'));
    ex("renderFlockSkill({recent_block_count:7,generated_detail_count:4,generated_summary_count:5,generated_fading_count:6,learning_state:'custom',state_text:'后端活动状态'})");assert.strictEqual(q('#waterAiSkillRecentCount').textContent,'近期记忆：7');assert.strictEqual(q('#waterAiSkillDetailCount').textContent,'详细记忆：4');assert.strictEqual(q('#waterAiSkillSummaryCount').textContent,'摘要记忆：5');assert.strictEqual(q('#waterAiSkillFadingCount').textContent,'淡化记忆：6');assert.strictEqual(q('#waterAiSkillStateTitle').textContent,'后端活动状态');
    ex("selectedAccount='distill-a';accountEpoch++;waterAISkillBusy=false");let done,loads=0;app.api=()=>new Promise(r=>done=r);app.loadFlockSkill=async()=>{loads++};const pending=ex("runFlockSkillAction('distill-now')");assert(q('#waterAiDistillNow').disabled);ex("selectedAccount='distill-b';accountEpoch++;resetWaterAIState()");q('#waterAiSkillStatus').textContent='新账号状态';done({distilled:true});await pending;assert.strictEqual(q('#waterAiSkillStatus').textContent,'新账号状态');assert.strictEqual(loads,0);assert.strictEqual(ex('waterAISkillBusy'),false);
    app.api=async()=>{throw new Error('mock distill unavailable')};app.loadFlockSkill=async()=>{};await ex("runFlockSkillAction('distill-now')");assert(q('#waterAiSkillStatus').textContent.includes('提炼失败：mock distill unavailable'));assert(!q('#waterAiDistillNow').disabled);
  });
  await test('学习卡片390与430宽度静态规则阻止文件和网格横向溢出',async()=>{
    for(const css of ['#flockLearningCard{min-width:0;overflow:hidden}','#flockLearningCard .fold-b','#flockLearningCard input[type=datetime-local]','#flockLearningCard input[type=file]{display:block;width:100%;overflow:hidden}','input[type=file]::file-selector-button','@media(max-width:430px)','grid-template-columns:repeat(2,minmax(0,1fr))','@media(max-width:340px)'])assert(html.includes(css));assert(html.includes('#flockLearningCard .water-ai-toolbar,#flockLearningCard .water-ai-stats{display:grid'));assert(html.includes('.learning-exam-result{max-width:100%'));assert(html.includes('.model-choice-list{width:100%;max-width:100%'));assert(html.includes('.ai-flock-emojis{display:grid;grid-template-columns:repeat(auto-fill,minmax(44px,1fr))'));assert(html.includes('.water-ai-limits>div{min-width:0}'));
  });
  await test('skill Markdown 选择只读取，校验大小UTF-8并在显式确认后导入且处理竞态换账号',async()=>{
    const sent=[];app.api=async(p,o)=>{sent.push([p,o]);return {ok:true}};app.loadFlockSkill=async()=>{};app.window.confirm=()=>true;
    const card=html.slice(html.indexOf('id="flockLearningCard"'),html.indexOf('id="migrationCard"'));assert(card.includes('选择 Markdown 文件（最大 5 MiB）')&&!card.includes('最大 64 KiB'));
    for(const file of [{name:'x.txt',size:10},{name:'x.md',size:0},{name:'x.md',size:5*1024*1024+1}]){q('#waterAiSkillFile').files=[file];ex('readFlockSkillFile()');assert(q('#waterAiSkillImport').disabled)}
    q('#waterAiSkillFile').files=[{name:'old.md',size:10}];ex('readFlockSkillFile()');const old=readers.at(-1);q('#waterAiSkillFile').files=[{name:'new.markdown',size:5*1024*1024}];ex('readFlockSkillFile()');const latest=readers.at(-1);assert(old.aborted);latest.complete('# 新人格');old.complete('# 旧人格');assert(!q('#waterAiSkillImport').disabled);assert.strictEqual(sent.length,0);assert(q('#waterAiSkillStatus').textContent.includes('确认导入'));
    app.window.confirm=()=>false;await ex('importFlockSkill()');assert.strictEqual(sent.length,0);app.window.confirm=()=>true;await ex('importFlockSkill()');assert.strictEqual(sent.length,1);assert.strictEqual(sent[0][0],'/api/ai/flock/skill/import');assert.strictEqual(sent[0][1].body,'# 新人格');assert.strictEqual(sent[0][1].headers['Content-Type'],'text/markdown; charset=utf-8');assert.strictEqual(q('#waterAiSkillFile').value,'');
    q('#waterAiSkillFile').files=[{name:'bad.md',size:8}];ex('readFlockSkillFile()');readers.at(-1).complete('# \uFFFD');assert(q('#waterAiSkillImport').disabled);assert(q('#waterAiSkillStatus').textContent.includes('UTF-8'));
    ex("selectedAccount='file-a';accountEpoch++");q('#waterAiSkillFile').files=[{name:'race.md',size:8}];ex('readFlockSkillFile()');const race=readers.at(-1);ex("selectedAccount='file-b';accountEpoch++;resetWaterAIState()");race.complete('# 迟到');assert(q('#waterAiSkillImport').disabled);assert.strictEqual(ex('waterAISkillDraft'),null);
  });
  await test('skill 导出下载附件携带冻结账号且不展示内容',async()=>{
    ex("selectedAccount='export-skill';accountEpoch++;waterAISkillBusy=false");let request;app.fetch=async(p,o)=>{request=[p,o];return {ok:true,status:200,blob:async()=>new Blob(['# private skill'],{type:'text/markdown'})}};const before=downloaded.length;await ex('exportFlockSkill()');assert.strictEqual(request[0],'/api/ai/flock/skill/export');assert.strictEqual(request[1].headers['X-Account-ID'],'export-skill');assert.strictEqual(downloaded.length,before+1);assert.strictEqual(downloaded.at(-1).download,'skill.md');assert(!q('#waterAiSkillStatus').textContent.includes('private skill'));
  });
  await test('study文件位于AI学习卡片并支持确认导入和账号隔离导出',async()=>{
    const card=html.slice(html.indexOf('id="flockLearningCard"'),html.indexOf('id="migrationCard"'));for(const id of ['waterAiStudySize','waterAiStudyExport','waterAiStudyFile','waterAiStudyImport'])assert(card.includes('id="'+id+'"'));assert(card.includes('定时上传 skill.md 与 study.md 到 Telegram 收藏夹'));
    ex("selectedAccount='study-a';accountEpoch++;waterAISkillBusy=false");app.loadFlockSkill=async()=>{};const sent=[];app.api=async(p,o)=>{sent.push([p,o]);return {ok:true}};q('#waterAiStudyFile').files=[{name:'study.md',size:12}];ex('readStudyFile()');readers.at(-1).complete('# 群内氛围');app.window.confirm=()=>true;await ex('importStudy()');assert.strictEqual(sent[0][0],'/api/ai/flock/study/import');assert.strictEqual(sent[0][1].body,'# 群内氛围');assert(q('#waterAiStudyImport').disabled);
    let request;app.fetch=async(p,o)=>{request=[p,o];return {ok:true,status:200,blob:async()=>new Blob(['# 群内氛围'])}};await ex('exportStudy()');assert.strictEqual(request[0],'/api/ai/flock/study/export');assert.strictEqual(request[1].headers['X-Account-ID'],'study-a');assert.strictEqual(downloaded.at(-1).download,'study.md');
  });
  await test('四个水群编辑器明确引用study和knowledge且预算往返准确',async()=>{
    assert.strictEqual((html.match(/study\.md \+ knowledge\.md 引用字数（0 不引用）/g)||[]).length,4);assert.strictEqual((html.match(/两份文件引用无上限/g)||[]).length,4);assert.strictEqual((html.match(/按需引用两份文件/g)||[]).length,4);
    assert.strictEqual(ex('taskConfig({ai_flock_knowledge_chars:-1}).ai_flock_knowledge_chars'),-1);assert.notStrictEqual(ex('taskFingerprint({ai_flock_knowledge_chars:-1})'),ex('taskFingerprint({ai_flock_knowledge_chars:0})'));
    for(const prefix of ['waterAiJob','brAiFlock','lkAiFlock','crAiFlock']){assert(html.includes('id="'+prefix+'KnowledgeOnDemand"'));ex("setIntegerFields('"+prefix+"',{},AI_FLOCK_LIMIT_SPECS)");q('#'+prefix+'KnowledgeChars').value='4567';q('#'+prefix+'KnowledgeUnlimited').checked=true;ex("syncKnowledgeLimit('"+prefix+"')");assert(q('#'+prefix+'KnowledgeChars').disabled);assert.strictEqual(ex("readIntegerFields('"+prefix+"',AI_FLOCK_LIMIT_SPECS).knowledge_chars"),-1);q('#'+prefix+'KnowledgeUnlimited').checked=false;ex("syncKnowledgeLimit('"+prefix+"')");assert.strictEqual(ex("readIntegerFields('"+prefix+"',AI_FLOCK_LIMIT_SPECS).knowledge_chars"),4567);ex("setIntegerFields('"+prefix+"',{knowledge_chars:-1},AI_FLOCK_LIMIT_SPECS)");assert(q('#'+prefix+'KnowledgeUnlimited').checked);ex("setIntegerFields('"+prefix+"',{},AI_FLOCK_LIMIT_SPECS)");assert(!q('#'+prefix+'KnowledgeUnlimited').checked);assert.strictEqual(q('#'+prefix+'KnowledgeChars').value,'2000')}
  });
  await test('四个水群编辑器按需知识开关独立保存回填和重置',async()=>{
    const models={waterAiJob:'waterAIJobPayload',brAiFlock:null};
    ex("selectedAccount='demand';accountEpoch++;watchedGroups=[{chat_id:'-1001',name:'群'}];aiProviders=[{id:'0123456789abcdef',enabled:true}];waterAIJobs=[{id:88,name:'按需任务',enabled:1,groups:'-1001',start_time:'09:00',end_time:'10:00',prompt:'聊',provider_ids:'0123456789abcdef',knowledge_on_demand:1}];openWaterAIJobForm(88)");assert(q('#waterAiJobKnowledgeOnDemand').checked);assert.strictEqual(JSON.parse(JSON.stringify(ex('waterAIJobPayload(waterAIJobs[0])'))).knowledge_on_demand,1);ex('resetWaterAIJobForm()');assert(!q('#waterAiJobKnowledgeOnDemand').checked);
    for(const [type,prefix] of [['button_register','br'],['link_register','lk'],['code_register','cr']]){ex("selectedAccount='auto-"+type+"';accountEpoch++;resetAccountCaches();openAutoForm('"+type+"')");assert(!q('#'+prefix+'AiFlockKnowledgeOnDemand').checked);q('#'+prefix+'AiFlockKnowledgeOnDemand').checked=true;q('#'+prefix+'AiFlock').checked=true;q('#'+prefix+'AiFlockKnowledgeChars').value='500';ex("closeAutoForm('"+type+"');openAutoForm('"+type+"')");assert(!q('#'+prefix+'AiFlockKnowledgeOnDemand').checked)}
  });
  await test('知识编辑默认收起，点击展开不聚焦，收起留草稿且保存后收起',async()=>{
    assert(html.includes('id="knowledgeEditFields" class="hidden"'));const textarea=html.match(/<textarea\b[^>]*id="knowledgeEditor"[^>]*>/)[0];assert(!textarea.includes('autofocus'));
    ex("selectedAccount='knowledge-fold';accountEpoch++;resetKnowledge()");app.api=async()=>({content:'已有知识',bytes:12});await ex('loadKnowledge()');assert(q('#knowledgeEditFields').classList.contains('hidden'));assert.strictEqual(q('#knowledgeEdit').getAttribute('aria-expanded'),'false');let focused=0;const focus=q('#knowledgeEditor').focus;q('#knowledgeEditor').focus=()=>focused++;
    ex('toggleKnowledgeEditor()');assert(!q('#knowledgeEditFields').classList.contains('hidden'));assert.strictEqual(q('#knowledgeEdit').getAttribute('aria-expanded'),'true');assert.strictEqual(focused,0);q('#knowledgeEditor').value='未保存草稿';ex('knowledgeChanged();toggleKnowledgeEditor();toggleKnowledgeEditor()');assert.strictEqual(q('#knowledgeEditor').value,'未保存草稿');assert(ex('knowledgeDirty'));assert.strictEqual(focused,0);app.api=async()=>({ok:true,bytes:15});await ex('saveKnowledge()');assert(q('#knowledgeEditFields').classList.contains('hidden'));assert.strictEqual(q('#knowledgeEdit').textContent,'编辑知识');ex('toggleKnowledgeEditor();resetKnowledge()');assert(q('#knowledgeEditFields').classList.contains('hidden'));q('#knowledgeEditor').focus=focus;
  });
  await test('知识编辑独立保存并安全显示，忙时防重复且保存不丢新输入',async()=>{
    ex("selectedAccount='knowledge-a';accountEpoch++;resetKnowledge()");app.api=async(p)=>{assert.strictEqual(p,'/api/ai/flock/knowledge');return {content:'# <script>知识</script>',bytes:30}};await ex('loadKnowledge()');assert.strictEqual(q('#knowledgeEditor').value,'# <script>知识</script>');assert.strictEqual(q('#knowledgeEditor').innerHTML,'');assert(!q('#knowledgeSave').disabled);
    q('#knowledgeEditor').value='# 新知识';ex('knowledgeChanged()');let done,sent=0;app.api=(p,o)=>{sent++;assert.strictEqual(p,'/api/ai/flock/knowledge');assert.strictEqual(o.method,'PUT');assert.strictEqual(JSON.parse(o.body).content,'# 新知识');return new Promise(r=>done=r)};const pending=ex('saveKnowledge()');await ex('saveKnowledge()');assert.strictEqual(sent,1);q('#knowledgeEditor').value='# 更新输入';ex('knowledgeChanged()');done({ok:true,bytes:10});await pending;assert.strictEqual(q('#knowledgeEditor').value,'# 更新输入');assert(ex('knowledgeDirty'));assert(q('#knowledgeStatus').textContent.includes('尚未保存'));
  });
  await test('知识读取及保存切换账号时忽略旧响应，不自动刷新覆盖草稿',async()=>{
    ex("selectedAccount='knowledge-old';accountEpoch++;resetKnowledge()");let done;app.api=()=>new Promise(r=>done=r);const old=ex('loadKnowledge()');ex("selectedAccount='knowledge-new';accountEpoch++;resetKnowledge()");done({content:'旧账号知识'});await old;assert.strictEqual(q('#knowledgeEditor').value,'');assert(!ex('knowledgeLoaded'));
    app.api=async()=>({content:'新账号知识',bytes:20});await ex('loadKnowledge()');q('#knowledgeEditor').value='未保存草稿';ex('knowledgeChanged()');let sent=0;app.api=async()=>{sent++;return {content:'服务器内容'}};await ex('loadKnowledge()');assert.strictEqual(sent,0);assert.strictEqual(q('#knowledgeEditor').value,'未保存草稿');
    app.api=()=>new Promise(r=>done=r);const reload=ex('loadKnowledge(true)');q('#knowledgeEditor').value='载入期间的新输入';ex('knowledgeChanged()');done({content:'载入前的服务器内容'});await reload;assert.strictEqual(q('#knowledgeEditor').value,'载入期间的新输入');assert(ex('knowledgeDirty'));
    app.api=()=>new Promise(r=>done=r);const save=ex('saveKnowledge()');ex("selectedAccount='knowledge-third';accountEpoch++;resetKnowledge()");done({ok:true});await save;assert.strictEqual(q('#knowledgeEditor').value,'');assert(!ex('knowledgeLoaded'));
  });
  await test('知识导入先本地读取再确认，校验格式大小UTF8并丢弃旧文件',async()=>{
    ex("selectedAccount='knowledge-import';accountEpoch++;resetKnowledge()");app.api=async()=>({content:'已有知识'});await ex('loadKnowledge()');for(const file of [{name:'x.txt',size:10},{name:'x.md',size:0},{name:'x.md',size:5*1024*1024+1}]){q('#knowledgeFile').files=[file];ex('readKnowledgeFile()');assert(q('#knowledgeImport').disabled)}
    q('#knowledgeFile').files=[{name:'old.md',size:10}];ex('readKnowledgeFile()');const old=readers.at(-1);q('#knowledgeFile').files=[{name:'new.md',size:5*1024*1024}];ex('readKnowledgeFile()');const latest=readers.at(-1);assert(old.aborted);latest.complete('# 导入知识');old.complete('旧文件');let sent=0;app.api=async(p,o)=>{sent++;assert.strictEqual(p,'/api/ai/flock/knowledge/import');assert.strictEqual(o.body,'# 导入知识');return {ok:true,bytes:20}};app.window.confirm=()=>false;await ex('importKnowledge()');assert.strictEqual(sent,0);app.window.confirm=()=>true;await ex('importKnowledge()');assert.strictEqual(sent,1);assert.strictEqual(q('#knowledgeEditor').value,'# 导入知识');assert(!ex('knowledgeDirty'));
    q('#knowledgeFile').files=[{name:'bad.md',size:10}];ex('readKnowledgeFile()');readers.at(-1).complete('\uFFFD');assert(q('#knowledgeImport').disabled);
  });
  await test('知识导出和收藏夹上传只使用已保存文档并保留账号范围',async()=>{
    ex("selectedAccount='knowledge-export';accountEpoch++;resetKnowledge()");app.api=async()=>({content:'# 知识',bytes:8});await ex('loadKnowledge()');let request;app.fetch=async(p,o)=>{request=[p,o];return {ok:true,status:200,blob:async()=>new Blob(['# 知识'])}};await ex('exportKnowledge()');assert.strictEqual(request[0],'/api/ai/flock/knowledge/export');assert.strictEqual(request[1].headers['X-Account-ID'],'knowledge-export');assert.strictEqual(downloaded.at(-1).download,'knowledge.md');let sent=0;app.api=async(p,o)=>{sent++;assert.strictEqual(p,'/api/ai/flock/knowledge/upload-now');assert.strictEqual(o._account.id,'knowledge-export');return {ok:true,date:'2026-11-12'}};await ex('uploadKnowledge()');assert.strictEqual(sent,1);assert(q('#knowledgeStatus').textContent.includes('已上传'));ex('knowledgeChanged()');await ex('uploadKnowledge()');assert.strictEqual(sent,1);
  });
  await test('AI API 模型发现使用未保存地址密钥和编辑ID，忙时防重复并安全填充列表',async()=>{
    ex("selectedAccount='models-a';accountEpoch++;aiProviders=[{id:'0123456789abcdef',note:'主线路',base:'https://saved.invalid/v1',model:'saved',enabled:true,key_present:true}];openAIProviderForm('0123456789abcdef')");q('#aiProviderBase').value='https://unsaved.invalid/v1';q('#aiProviderKey').value='';let blocked=0;app.api=async()=>{blocked++;return {models:[]}};await ex('discoverAIProviderModels()');assert.strictEqual(blocked,0);assert(q('#aiProviderModelsStatus').textContent.includes('地址已修改'));assert(q('#aiProviderModelsStatus').textContent.includes('请输入 API Key'));assert(!q('#aiProviderModelsBtn').disabled);
    q('#aiProviderKey').value='temporary-secret';q('#aiProviderModel').value='手填模型';let resolve,calls=0,body;app.api=(p,o)=>{calls++;assert.strictEqual(p,'/api/ai/providers/models');body=JSON.parse(o.body);return new Promise(r=>resolve=r)};const pending=ex('discoverAIProviderModels()');await ex('discoverAIProviderModels()');assert.strictEqual(calls,1);assert(q('#aiProviderModelsBtn').disabled);assert.deepStrictEqual(body,{base:'https://unsaved.invalid/v1',key:'temporary-secret',provider_id:'0123456789abcdef'});resolve({models:['safe-model','<img onerror=bad()>','safe-model']});await pending;assert.strictEqual(q('#aiProviderModel').value,'手填模型');assert.deepStrictEqual(q('#aiProviderModelOptions').children.map(x=>x.value),['safe-model','<img onerror=bad()>']);assert.strictEqual(q('#aiProviderModelOptions').innerHTML,'');assert(!q('#aiProviderModelsBtn').disabled);
    app.api=async()=>{throw new Error('denied temporary-secret')};await ex('discoverAIProviderModels()');assert(q('#aiProviderModelsStatus').textContent.includes('[已隐藏]'));assert(!q('#aiProviderModelsStatus').textContent.includes('temporary-secret'));
  });
  await test('模型发现表单变化和账号切换丢弃旧结果，新增不发送provider_id',async()=>{
    ex("selectedAccount='models-b';accountEpoch++;openAIProviderForm()");q('#aiProviderBase').value='https://new.invalid/v1';q('#aiProviderKey').value='new-key';let resolve,body;app.api=(_p,o)=>{body=JSON.parse(o.body);return new Promise(r=>resolve=r)};const pending=ex('discoverAIProviderModels()');q('#aiProviderBase').value='https://changed.invalid/v1';resolve({models:['stale-model']});await pending;assert.strictEqual(q('#aiProviderModelOptions').children.length,0);assert(!('provider_id' in body));
    let done;app.api=()=>new Promise(r=>done=r);q('#aiProviderBase').value='https://account.invalid/v1';const accountPending=ex('discoverAIProviderModels()');ex("selectedAccount='models-c';accountEpoch++;closeAIProviderForm()");done({models:['other-account-model']});await accountPending;assert.strictEqual(q('#aiProviderModelOptions').children.length,0);
  });
  await test('模型发现同时提供移动端可见十项列表并安全回填和清理',async()=>{
    ex("selectedAccount='models-visible';accountEpoch++;openAIProviderForm()");q('#aiProviderBase').value='https://models.invalid/v1';q('#aiProviderKey').value='model-key';app.modelList=Array.from({length:10},(_,i)=>i===4?'<img src=x onerror=bad()>':'model-'+(i+1));app.api=async()=>({models:app.modelList});await ex('discoverAIProviderModels()');assert.strictEqual(q('#aiProviderModelOptions').children.length,10);assert.strictEqual(q('#aiProviderModelList').children.length,10);assert(!q('#aiProviderModelList').classList.contains('hidden'));assert.strictEqual(q('#aiProviderModelList').children[4].textContent,'<img src=x onerror=bad()>');assert.strictEqual(q('#aiProviderModelList').innerHTML,'');q('#aiProviderModelList').value='<img src=x onerror=bad()>';ex('selectAIProviderModel()');assert.strictEqual(q('#aiProviderModel').value,'<img src=x onerror=bad()>');assert(q('#aiProviderModelsStatus').textContent.includes('<img src=x onerror=bad()>'));
    ex('clearAIProviderModelChoices()');assert.strictEqual(q('#aiProviderModelList').children.length,0);assert(q('#aiProviderModelList').classList.contains('hidden'));assert(!q('#aiProviderModelsBtn').disabled);assert(html.includes('size="10"')&&html.includes('oninput="clearAIProviderModelChoices()"'));assert(html.includes('.model-choice-list option{min-height:44px'));
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
    let tick,states=0,logs=0,skills=0;app.setInterval=fn=>{tick=fn;return 99};app.clearInterval=()=>{};app.api=async()=>({});
    app.loadState=()=>{states++};app.loadLogs=()=>{logs++};app.loadFlockSkill=()=>{skills++};for(const name of ['loadAccounts','loadSettings','loadRules','loadAutos','loadRuleGroups','renderAccounts','renderEmojis','pickAction','pickGrabAct','applyTheme'])app[name]=()=>{};
    ex("activeTab='logs';bootstrapTimer=null");await ex('bootstrap()');states=logs=0;
    ex("document.visibilityState='hidden'");tick();assert.strictEqual(states,0);assert.strictEqual(logs,0);assert.strictEqual(skills,0);ex('document.listeners.visibilitychange()');assert.strictEqual(states,0);assert.strictEqual(logs,0);
    ex("document.visibilityState='visible'");assert.strictEqual(ex('pageIsHidden()'),false);ex('refreshVisibleTab()');assert.strictEqual(states,1);assert.strictEqual(logs,1);assert(js.includes("addEventListener('visibilitychange',()=>{if(!pageIsHidden())refreshVisibleTab()})"));tick();assert.strictEqual(states,2);assert.strictEqual(logs,2);assert.strictEqual(skills,0);ex("activeTab='settings';waterAISkillBusy=false;waterAISkillLoading=false");tick();assert.strictEqual(skills,1);ex('waterAISkillBusy=true');tick();assert.strictEqual(skills,1);ex("waterAISkillBusy=false;waterAISkillLoading=true");tick();assert.strictEqual(skills,1);ex("waterAISkillLoading=false;activeTab='autos'");tick();assert.strictEqual(skills,1);assert(js.includes('setInterval(pollVisibleState,5000)'));assert(js.includes('loadFlockSkill(true)'));
  });
  await test('日志导出CSV和加载更早携带分类搜索与游标',async()=>{
    assert(html.includes('onclick="exportLogs()"')&&html.includes('id="logMore"'));
    ex("selectedAccount='log-more';accountEpoch++;logKind='signin';logQ='x';logBefore=0;logHasMore=false");
    assert(js.includes("'/api/logs/export?kind='+encodeURIComponent(logKind)"));assert(js.includes("logQ?('&q='+encodeURIComponent(logQ))"));assert(js.includes("link.download='tg-persona-logs.csv'"));
    const req=[];app.api=async(p,o)=>{req.push([p,o]);return {hits:[{id:42,ts:1,action:'signin',ok:1,detail:{}}],has_more:true}};
    ex("logOpenIds.clear();document.querySelector('#logList').innerHTML='';logBefore=99;logHasMore=true");
    await ex('loadMoreLogs()');assert(req[0][0].includes('before=99')&&req[0][0].includes('n=80'));assert.strictEqual(ex('logBefore'),42);assert(ex('logHasMore'));
    ex('logBefore=0;logHasMore=false;loadMoreLogs()');assert.strictEqual(req.length,1);
  });
  await test('AI用量按日功能线路展示且失败不破坏页面',async()=>{
    ex("selectedAccount='usage';accountEpoch++;aiUsageBusy=false");app.api=async p=>{assert.strictEqual(p,'/api/ai/usage?days=14');return {days:[{day:'2026-11-12',input_tokens:12,output_tokens:3}],features:[{feature:'ai_flock',input_tokens:12,output_tokens:3}],providers:[{note:'主线路',input_tokens:12,output_tokens:3}]}};
    await ex('loadAIUsage()');const body=q('#aiUsageBody');assert.strictEqual(body.children.length,1);assert.strictEqual(body.innerHTML,'');const wrap=body.children[0],labels=[...new Set((wrap.children||[]).map(node=>node.textContent))];assert(labels.some(v=>String(v).includes('最近 14 天')));assert(labels.some(v=>String(v).includes('AI 水群')));assert(labels.some(v=>String(v).includes('主线路')));
    app.api=async()=>{throw new Error('mock usage error')};await ex('loadAIUsage()');assert(q('#aiUsageBody').textContent.includes('用量加载失败'));
  });
  await test('知识引用预览只读提交文本字数与按需模式',async()=>{
    ex("selectedAccount='kp';accountEpoch++;aiProvidersLoaded=true");q('#knowledgePreviewText').value='CF 522 怎么办';q('#knowledgePreviewChars').value='500';q('#knowledgePreviewOnDemand').value='1';
    let request;app.api=async(p,o)=>{request=[p,o];return {mode:'relevant',chars:500,reason:'提问或求助，引用约 500 字',preview:'知识片段'}};
    await ex('previewKnowledge()');assert.strictEqual(request[0],'/api/ai/flock/knowledge/preview');assert.deepStrictEqual(JSON.parse(request[1].body),{text:'CF 522 怎么办',knowledge_chars:500,knowledge_on_demand:1});assert.strictEqual(q('#knowledgePreviewResult').textContent,'提问或求助，引用约 500 字\n\n知识片段');assert(!q('#knowledgePreviewBtn').disabled);
    app.api=async()=>({mode:'',chars:0,preview:'',reason:'该消息不会引用知识'});await ex('previewKnowledge()');assert.strictEqual(q('#knowledgePreviewResult').textContent,'该消息不会引用知识');
  });
  await test('自动备份保存账号时间密码备注且立即备份提示结果',async()=>{
    ex("selectedAccount='backup';accountEpoch++;autoBackupBusy=false");app.api=async p=>({enabled:true,time:'04:30',account:'default',caption_password:true,password_present:true,last_run:'2026-11-12 04:30',last_error:'',accounts:[{id:'default',label:'默认',logged_in:true,phase:'ready'},{id:'second',label:'小号',logged_in:false,phase:'idle'}]});
    await ex('loadAutoBackup()');assert(q('#autoBackupEnabled').checked);assert.strictEqual(q('#autoBackupPassword').value,'');assert(q('#autoBackupPassword').placeholder.includes('留空不修改'));
    const options=q('#autoBackupAccount').children;assert.strictEqual(options.length,2);assert.strictEqual(options[0].value,'default');assert(options[1].textContent.includes('小号'));
    let request;app.api=async(p,o)=>{if(o&&o.method==='PUT')request=[p,o];return o&&o.method==='PUT'?{ok:true}:{enabled:false,accounts:[]}};ex('autoBackupBusy=false');q('#autoBackupTime').value='05:15';q('#autoBackupAccount').value='second';q('#autoBackupCaptionPassword').checked=true;q('#autoBackupPassword').value='secret123';
    await ex('saveAutoBackup()');assert.strictEqual(request[0],'/api/migration/auto-backup');assert.deepStrictEqual(JSON.parse(request[1].body),{enabled:1,time:'05:15',account:'second',caption_password:1,password:'secret123'});
    let ran=false;app.api=async(p,o)=>{if(p==='/api/migration/auto-backup/run'){ran=true;return {ok:true,file:'tg-persona-migration-1.tgp',removed:2}}return {enabled:true,time:'05:15',account:'second',caption_password:true,password_present:true,last_run:'2026-11-12 05:15',last_error:'',accounts:[{id:'default',label:'默认',logged_in:true}]}};app.window.confirm=()=>true;await ex('runAutoBackupNow()');assert(ran);assert(q('#autoBackupStatus').textContent.includes('2026-11-12 05:15'));assert(!q('#autoBackupNow').disabled);
    ex('autoBackupBusy=false');q('#autoBackupEnabled').checked=false;ex('syncAutoBackupForm()');assert(q('#autoBackupFields').classList.contains('hidden'));
  });
  await test('日志轮询重绘保留当前展开详情',async()=>{
    assert(js.includes('let logOpenIds=new Set()'));
    assert(js.includes("open=logOpenIds.has(logId)?' open':''"));
    assert(js.includes("item.classList.toggle('open')"));
    assert(js.includes('data-log-id='));
    assert(!js.includes('class="logitem" onclick="this.classList.toggle'));
  });
  await test('日志清理操作在列表上方吸顶且手机触控尺寸足够',async()=>{
    const start=html.indexOf('<section id="tab-logs">'),end=html.indexOf('</section>',start),part=html.slice(start,end);
    const toolbar=part.indexOf('class="log-toolbar"'),list=part.indexOf('id="logList"');assert(toolbar>0&&toolbar<list);
    assert.strictEqual((part.match(/onclick="clearLogs\(/g)||[]).length,2);assert(part.includes('class="danger" onclick="clearLogs(0)"'));
    assert(html.includes('.log-toolbar{position:sticky'));assert(html.includes('.log-toolbar input,.log-toolbar button{min-height:44px}'));assert(html.includes('overflow-x:hidden;overflow-x:clip'));
  });
  await test('系统更新位于设置最顶部且默认收起',async()=>{
    const start=html.indexOf('<section id="tab-settings">'),end=html.indexOf('</section>',start);
    const part=html.slice(start,end),update=part.indexOf('系统更新'),accounts=part.indexOf('Telegram 账号');
    assert(update>0&&accounts>update);
    const before=part.slice(Math.max(0,update-300),update);
    assert(before.includes('class="card fold"'));
    assert(!before.includes('class="card fold open"'));
  });
  await test('品牌显示当前版本并在每次面板启动静默检测更新',async()=>{
    assert(html.includes('id="brandVersion"'));
    assert(html.includes('id="brandUpdate"'));
    assert(js.includes('function renderBrandVersion(s)'));
    assert(js.includes("autoCheckVersion();"));
    assert(js.includes("await loadUpd(true)"));
  });
  console.log(`前端回归 ${count} 项全部通过`);
})().catch(e=>{console.error(e);process.exitCode=1});
