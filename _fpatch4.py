import re

p = 'web/index.html'
s = open(p).read()

# ---------- 1. 头像:不再每 5 秒重新加载 ----------
old_av = """function setAvatar(el, custom, name){
  if(!el)return;
  el.innerHTML='';
  const letter=document.createElement('span');
  letter.textContent=(name||'?').charAt(0).toUpperCase();
  const img=document.createElement('img');
  let step=0;
  const srcs=[custom, '/avatar?v='+Date.now()].filter(Boolean);
  const next=()=>{
    if(step>=srcs.length){el.innerHTML='';el.appendChild(letter);return}
    img.src=srcs[step++];
  };
  img.onerror=next;
  if(srcs.length){next();el.appendChild(img)}else{el.appendChild(letter)}
}"""
new_av = """function setAvatar(el, custom, name){
  if(!el)return;
  // 同一个头像不重复设置,避免轮询刷新时图片反复重载
  const key=(custom||'/avatar')+'|'+(state&&state.tg&&state.tg.me?state.tg.me.id:'');
  if(el.dataset.avkey===key&&el.querySelector('img'))return;
  el.dataset.avkey=key;
  el.innerHTML='';
  const letter=document.createElement('span');
  letter.textContent=(name||'?').charAt(0).toUpperCase();
  const img=document.createElement('img');
  // 自定义 URL 失败 → 回退 /avatar(TG 头像缓存) → 首字母
  const srcs=custom?[custom,'/avatar']:['/avatar'];
  let step=0;
  const next=()=>{
    if(step>=srcs.length){el.innerHTML='';el.appendChild(letter);return}
    img.src=srcs[step++];
  };
  img.onerror=next;
  next();
  el.appendChild(img);
}"""
assert old_av in s
s = s.replace(old_av, new_av, 1)

# ---------- 2. 头像在 .flex 里被拉伸/裁切 ----------
old_css = ".avatar.lg{width:54px;height:54px;font-size:23px}"
new_css = (".avatar.lg{width:54px;height:54px;font-size:23px}\n"
           ".flex>.avatar{flex:0 0 auto;min-width:0;width:48px;height:48px}\n"
           ".flex>.avatar img{width:100%;height:100%;object-fit:cover}")
assert old_css in s
s = s.replace(old_css, new_css, 1)

# ---------- 3. 设置页不再强制展开 Telegram 卡片 ----------
old_tab = "  if(t==='settings'){const f=$('#foldTg');if(f&&!f.classList.contains('open'))f.classList.add('open');renderTgCard()}"
new_tab = "  if(t==='settings'){renderTgCard()}"
assert old_tab in s
s = s.replace(old_tab, new_tab, 1)

# ---------- 4. 日志「保号」→「群聊」 ----------
s = s.replace('<button data-k="keepalive" onclick="pickLogKind(\'keepalive\')">保号</button>',
              '<button data-k="keepalive" onclick="pickLogKind(\'keepalive\')">群聊</button>', 1)
s = s.replace("keepalive:'保号'}", "keepalive:'群聊'}", 1)
s = s.replace("title=(d.task||'保号')", "title=(d.task||'群聊发言')", 1)

# ---------- 5. 自动化:表单改成弹窗 ----------
FORM_CSS = """.autof{display:none;position:fixed;inset:0;z-index:130;background:rgba(8,10,18,.55);padding:14px;overflow:auto;-webkit-overflow-scrolling:touch}
.autof.show{display:block}
.autof-card{background:var(--panel);border:1px solid var(--hair);border-radius:16px;padding:16px;max-width:560px;margin:10px auto 40px;box-shadow:var(--sh-lg)}
.autof-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.autof-head b{font-size:15px}
.addtask{width:100%;margin:12px 0 6px;display:flex;align-items:center;justify-content:center;gap:6px}
"""
anchor = ".seg{display:flex;gap:6px;overflow:auto;padding:2px 0 10px}"
assert anchor in s
s = s.replace(anchor, FORM_CSS + anchor, 1)


def wrap_form(src, typ, title):
    start = src.index('<form onsubmit="saveAuto(\'%s\');return false">' % typ)
    end = src.index('</form>', start) + len('</form>')
    body = src[start:end]
    wrapped = ('<div class="autof" id="af-%s"><div class="autof-card">'
               '<div class="autof-head"><b>%s</b>'
               '<button class="ghost" type="button" onclick="closeAutoForm(\'%s\')">关闭</button></div>'
               '%s</div></div>') % (typ, title, typ, body)
    return src[:start] + wrapped + src[end:]


for typ, title in (('signin', '新增 / 编辑签到任务'), ('grabcodes', '新增 / 编辑抢注任务'),
                   ('custom', '新增 / 编辑自定义任务'), ('redpacket', '新增 / 编辑抢红包任务'),
                   ('keepalive', '新增 / 编辑群聊发言任务')):
    s = wrap_form(s, typ, title)

# 每张卡片顶部加「添加任务」按钮(插在 empty 那行之后)
for typ in ('signin', 'grabcodes', 'custom', 'redpacket', 'keepalive'):
    marker = {
        'signin': 'id="aEmptySign"', 'grabcodes': 'id="aEmptyGrab"', 'custom': 'id="aEmptyCustom"',
        'redpacket': 'id="aEmptyPack"', 'keepalive': 'id="aEmptyKeep"',
    }[typ]
    i = s.index(marker)
    j = s.index('</div>', i) + len('</div>')
    label = {'signin': '签到任务', 'grabcodes': '抢注任务', 'custom': '自定义任务',
             'redpacket': '抢红包任务', 'keepalive': '群聊发言任务'}[typ]
    btn = ('\n        <button class="btn addtask" type="button" onclick="openAutoForm(\'%s\')">＋ 添加%s</button>'
           % (typ, label))
    s = s[:j] + btn + s[j:]

# 打开/关闭函数 + saveAuto 成功后关闭 + editAutoItem 打开弹窗
old_save_hook = "async function saveAuto(type){"
new_save_hook = """function openAutoForm(type){
  const el=document.getElementById('af-'+type);
  if(!el)return;
  el.classList.add('show');
  document.body.style.overflow='hidden';
}
function closeAutoForm(type){
  const el=document.getElementById('af-'+type);
  if(el)el.classList.remove('show');
  document.body.style.overflow='';
  cancelAutoEdit(type);
}
async function saveAuto(type){"""
assert old_save_hook in s
s = s.replace(old_save_hook, new_save_hook, 1)

old_saved = "    cancelAutoEdit(type);toast(editAuto?'已更新':'已新增');editAuto=null;loadAutos();"
new_saved = "    closeAutoForm(type);toast(editAuto?'已更新':'已新增');editAuto=null;loadAutos();"
assert old_saved in s
s = s.replace(old_saved, new_saved, 1)

# editAutoItem 末尾:滚动改成打开弹窗
old_scroll = "  $(editAuto.type==='signin'?'#sName':editAuto.type==='grabcodes'?'#gName':editAuto.type==='custom'?'#cName':'#pName').scrollIntoView({behavior:'smooth'});"
new_scroll = ("  openAutoForm(editAuto.type);\n"
              "  const first={signin:'#sName',grabcodes:'#gName',custom:'#cName',redpacket:'#pName',keepalive:'#kName'}[editAuto.type];\n"
              "  const box=document.querySelector('#af-'+editAuto.type+' .autof-card');if(box)box.scrollIntoView({block:'start'});")
assert old_scroll in s
s = s.replace(old_scroll, new_scroll, 1)

open(p, 'w').write(s)
print('ui patch ok')
