'use strict';
// 原前端代码提取后运行；不访问服务器，不依赖第三方 DOM 库。
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(path.join(__dirname,'../web/index.html'),'utf8');
const js=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const elements=new Map(),calls=[],intervals=new Map(),readers=[];let intervalId=0;
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
  if(!elements.has(id))elements.set(id,{innerHTML:'',value:'',textContent:'',style:{},disabled:false,src:'',listeners:{},classList:{add(){},remove(){},toggle(){}},addEventListener(t,fn){this.listeners[t]=fn},contains(){return true},removeAttribute(k){delete this[k]}});
  return elements.get(id);
}
const context=vm.createContext({console,Buffer,Date,JSON,String,Error,Promise,FileReader:MockFileReader,
  fetch(){throw new Error('测试禁止真实网络请求')},
  document:{querySelector:element},
  toast:(...args)=>calls.push(['toast',...args]),
  api:async(...args)=>{calls.push(['api',...args]);return {}},
  addGroup:(...args)=>calls.push(['addGroup',...args]),removeGroup:id=>calls.push(['removeGroup',id]),
  loadSessions(){},bootstrap(){},
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
run(slice('async function reloadCaptcha(){','async function panelLogin'));
run(slice('async function changePw(){','async function logoutAll'));
run(slice('let updTimer=null','async function checkUpdate'));
run(slice('// ---------- AI 与验证码测试 ----------','async function changePw(){'));
for(const id of ['#aiTestResult','#captchaTestResult']){
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
  console.log(`前端回归 ${count} 项全部通过`);
})().catch(e=>{console.error(e);process.exitCode=1});
