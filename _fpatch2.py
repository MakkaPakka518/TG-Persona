p = 'web/index.html'
s = open(p).read()

# ---------- HTML:把两个文本输入换成 chip 选择 ----------
old_g = '''          <label>监控群(@群名或群ID,逗号分隔;留空 = 全部监听群)</label>
          <input type="text" id="gGroups" placeholder="如: @invite_chat, -1001234567890">'''
new_g = '''          <label>监控群</label>
          <div class="hint" style="margin:0 0 8px">不勾 = 全部监听群</div>
          <div id="gGroups" class="grp-pick"></div>'''
assert old_g in s
s = s.replace(old_g, new_g, 1)

old_p = '''          <label>监控群(@群名或群ID,逗号分隔;留空 = 全部监听群)</label>
          <input type="text" id="pGroups" placeholder="如: @redpacket_chat">'''
new_p = '''          <label>监控群</label>
          <div class="hint" style="margin:0 0 8px">不勾 = 全部监听群</div>
          <div id="pGroups" class="grp-pick"></div>'''
assert old_p in s
s = s.replace(old_p, new_p, 1)

# ---------- JS:选择状态 + 渲染 ----------
js_anchor = "async function saveAuto(type){"
picker = '''const autoGroupSel={grabcodes:new Set(),redpacket:new Set()};
function renderAutoGroups(type){
  const box=document.getElementById(type==='grabcodes'?'gGroups':'pGroups');
  if(!box)return;
  const sel=autoGroupSel[type];
  if(!watchedGroups.length){box.innerHTML='<div class="empty" style="padding:6px">还没有监听群,先去「群组」页添加</div>';return}
  box.innerHTML=watchedGroups.map(g=>`<div class="gp ${sel.has(String(g.chat_id))?'on':''}" onclick="toggleAutoGroup('${type}','${g.chat_id}')">${esc(g.name||g.chat_id)}</div>`).join('');
}
function toggleAutoGroup(type,id){
  id=String(id);const sel=autoGroupSel[type];
  sel.has(id)?sel.delete(id):sel.add(id);
  renderAutoGroups(type);
}
function setAutoGroups(type,csv){
  autoGroupSel[type]=new Set(String(csv||'').split(/[,，]/).map(x=>x.trim()).filter(Boolean));
  renderAutoGroups(type);
}
function getAutoGroups(type){return [...autoGroupSel[type]].join(',')}

'''
assert js_anchor in s
s = s.replace(js_anchor, picker + js_anchor, 1)

# ---------- editAutoItem:回填 chip ----------
old_e1 = "    $('#gName').value=a.name;$('#gGroups').value=a.groups;$('#gKeywords').value=a.keywords;"
new_e1 = "    $('#gName').value=a.name;setAutoGroups('grabcodes',a.groups);$('#gKeywords').value=a.keywords;"
assert old_e1 in s
s = s.replace(old_e1, new_e1, 1)

old_e2 = "    $('#pName').value=a.name;$('#pGroups').value=a.groups;$('#pKeywords').value=a.keywords;"
new_e2 = "    $('#pName').value=a.name;setAutoGroups('redpacket',a.groups);$('#pKeywords').value=a.keywords;"
assert old_e2 in s
s = s.replace(old_e2, new_e2, 1)

# ---------- cancelAutoEdit:清空 chip ----------
old_c1 = "$('#gName').value='';$('#gGroups').value='';"
new_c1 = "$('#gName').value='';setAutoGroups('grabcodes','');"
assert old_c1 in s
s = s.replace(old_c1, new_c1, 1)

old_c2 = "$('#pName').value='';$('#pGroups').value='';"
new_c2 = "$('#pName').value='';setAutoGroups('redpacket','');"
assert old_c2 in s
s = s.replace(old_c2, new_c2, 1)

# ---------- saveAuto:读取 chip ----------
old_s1 = "body={type,name:$('#gName').value.trim(),groups:$('#gGroups').value.trim(),keywords:$('#gKeywords').value.trim(),"
new_s1 = "body={type,name:$('#gName').value.trim(),groups:getAutoGroups('grabcodes'),keywords:$('#gKeywords').value.trim(),"
assert old_s1 in s
s = s.replace(old_s1, new_s1, 1)

old_s2 = "body={type,name:$('#pName').value.trim(),groups:$('#pGroups').value.trim(),keywords:$('#pKeywords').value.trim(),"
new_s2 = "body={type,name:$('#pName').value.trim(),groups:getAutoGroups('redpacket'),keywords:$('#pKeywords').value.trim(),"
assert old_s2 in s
s = s.replace(old_s2, new_s2, 1)

# ---------- loadRuleGroups 时顺带渲染两个 chip 区 ----------
old_l = "    const d=await api('/api/watched');watchedGroups=d.groups||[];renderRuleGroups();"
new_l = ("    const d=await api('/api/watched');watchedGroups=d.groups||[];renderRuleGroups();\n"
         "    renderAutoGroups('grabcodes');renderAutoGroups('redpacket');")
assert old_l in s
s = s.replace(old_l, new_l, 1)

open(p, 'w').write(s)
print('auto groups patch ok')
