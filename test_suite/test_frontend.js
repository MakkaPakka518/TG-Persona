'use strict';
// 原前端代码提取后运行；不访问服务器，不依赖第三方 DOM 库。
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(path.join(__dirname,'../web/index.html'),'utf8');
const js=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const elements=new Map(),calls=[],intervals=new Map();let intervalId=0;
function element(id){
  if(!elements.has(id))elements.set(id,{innerHTML:'',value:'',textContent:'',style:{},disabled:false,src:'',listeners:{},classList:{add(){},remove(){},toggle(){}},addEventListener(t,fn){this.listeners[t]=fn},contains(){return true},removeAttribute(k){delete this[k]}});
  return elements.get(id);
}
const context=vm.createContext({console,Buffer,Date,JSON,String,Error,Promise,
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
  console.log(`前端回归 ${count} 项全部通过`);
})().catch(e=>{console.error(e);process.exitCode=1});
