p = 'web/index.html'
s = open(p).read()

# 1) 浅色模式开关绿
old_css = 'html[data-theme="light"] .switch{background:#d4d9e4}'
new_css = old_css + '\nhtml[data-theme="light"] .switch.on{background:#22c55e}'
assert old_css in s
s = s.replace(old_css, new_css, 1)

# 2) 日志样式
css_anchor = '.grp-pick{display:flex;'
log_css = '''.seg{display:flex;gap:6px;overflow:auto;padding:2px 0 10px}
.seg button{flex:none;padding:8px 14px;border-radius:999px;border:1px solid var(--hair2);background:var(--panel2);
  color:var(--sub);font-size:12.5px;cursor:pointer}
.seg button.on{background:#22c55e;border-color:#22c55e;color:#fff}
.logitem{padding:11px 2px;border-bottom:1px solid var(--hair);cursor:pointer}
.logitem:last-child{border-bottom:0}
.logrow{display:flex;align-items:center;gap:9px}
.logrow .st{font-size:11.5px;padding:2px 7px;border-radius:6px;flex:none;font-weight:600}
.logrow .st.ok{background:rgba(34,197,94,.15);color:#16a34a}
.logrow .st.no{background:rgba(224,49,49,.14);color:#e03131}
.logrow .t{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13.5px}
.logrow .tm{font-size:11.5px;color:var(--dim);flex:none}
.logdetail{display:none;margin-top:9px;padding:10px 12px;background:var(--panel2);border-radius:10px;font-size:12.8px;line-height:1.6}
.logitem.open .logdetail{display:block}
.logdetail .k{color:var(--dim);margin-right:6px}
.logdetail .line{margin-bottom:4px;word-break:break-word}
''' + css_anchor
assert css_anchor in s
s = s.replace(css_anchor, log_css, 1)

# 3) 日志页 HTML
old_html = '''    <h2><svg viewBox="0 0 24 24"><path d="M1 8h22M4 16h10M8 4v16M16 4v16"/></svg>命中日志</h2>
    <div class="sub">最近触发的关键词动作记录</div>
    <div class="card" id="logList"></div>'''
new_html = '''    <h2><svg viewBox="0 0 24 24"><path d="M1 8h22M4 16h10M8 4v16M16 4v16"/></svg>日志</h2>
    <div class="sub">按类型分开 · 点一行展开完整记录</div>
    <div class="seg" id="logSeg">
      <button class="on" data-k="reply" onclick="pickLogKind('reply')">回复</button>
      <button data-k="signin" onclick="pickLogKind('signin')">签到</button>
      <button data-k="grabcodes" onclick="pickLogKind('grabcodes')">抢注</button>
      <button data-k="redpacket" onclick="pickLogKind('redpacket')">红包</button>
      <button data-k="custom" onclick="pickLogKind('custom')">自定义</button>
    </div>
    <div class="card" id="logList"></div>'''
assert old_html in s
s = s.replace(old_html, new_html, 1)

# 4) 日志 JS 整段替换
JS = r'''let logKind='reply';
const LOG_KINDS={reply:'回复',signin:'签到',grabcodes:'抢注',redpacket:'红包',custom:'自定义'};
function pickLogKind(k){
  logKind=k;
  document.querySelectorAll('#logSeg button').forEach(b=>b.classList.toggle('on',b.dataset.k===k));
  loadLogs();
}
function logRow(h){
  const ok=(h.ok===undefined?1:h.ok)?'ok':'no';
  const d=h.detail||{};
  let title='';
  if(h.action==='signin') title=(d.task||'签到')+(d.bot?(' · '+d.bot):'');
  else if(h.action==='custom') title=(d.task||'自定义任务');
  else if(h.action==='grabcodes'||h.action==='redpacket') title=(d.task||LOG_KINDS[h.action]||h.action)+(d.group?(' · '+d.group):'');
  else title=(d.group||h.grp_name||('群 '+h.chat_id))+(d.rule?(' · '+d.rule):'');
  const lines=[];
  const add=(k,v)=>{if(v)lines.push('<div class="line"><span class="k">'+k+'</span>'+esc(String(v))+'</div>')};
  if(h.action==='signin'){
    add('时间',new Date(h.ts*1000).toLocaleString('zh-CN'));
    add('目标',d.bot||h.chat_id);
    add('指令',d.command||h.content);
    add('回复',d.reply||h.reply);
    add('按钮',d.button);
    add('验证码',d.captcha);
    add('积分',d.points);
  }else if(h.action==='custom'){
    add('目标',d.bot||h.chat_id);
    add('过程',d.steps||h.reply);
  }else{
    add('群',d.group||h.grp_name||h.chat_id);
    add('发送人',d.sender||h.sender);
    add('消息',d.message||h.content);
    add('动作',d.action||(h.action==='react'?'点表情':h.action==='both'?'回复+表情':'回复'));
    add('回复',d.reply||h.reply||d.registered);
    add('规则',d.rule);
    add('指令',d.probe);
  }
  return '<div class="logitem" onclick="this.classList.toggle(\'open\')">'+
    '<div class="logrow"><span class="st '+ok+'">'+(ok==='ok'?'成功':'失败')+'</span>'+
    '<span class="t">'+esc(title)+'</span><span class="tm">'+fmt(h.ts)+'</span></div>'+
    '<div class="logdetail">'+(lines.join('')||'<div class="line">无详情</div>')+'</div></div>';
}
async function loadLogs(){
  try{
    const d=await api('/api/logs?n=80&kind='+logKind);
    const c=d.counts||{};
    document.querySelectorAll('#logSeg button').forEach(b=>{
      const n=c[b.dataset.k]||0;
      b.textContent=LOG_KINDS[b.dataset.k]+(n?' '+n:'');
    });
    document.getElementById('logList').innerHTML=d.hits.length?d.hits.map(logRow).join(''):'<div class="empty">这一类还没有记录</div>';
  }catch(e){toast(e.message,1)}
}

'''
start = s.index('async function loadLogs(){')
marker = '// ---------- 启动 ----------'
end = s.index(marker)
s = s[:start] + JS + s[end:]

open(p, 'w').write(s)
print('front patch ok, size', len(s))
