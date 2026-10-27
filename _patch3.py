P = 'main.py'
s = open(P).read()
n = 0


def rep(old, new):
    global s, n
    assert old in s, 'NOT FOUND: ' + old[:80]
    s = s.replace(old, new, 1)
    n += 1


# 1) hits 增加 ok / sender / detail
rep('    for col, ddl in (("content", "TEXT DEFAULT \'\'"), ("reply", "TEXT DEFAULT \'\'"), ("grp_name", "TEXT DEFAULT \'\'")):',
    '    for col, ddl in (("content", "TEXT DEFAULT \'\'"), ("reply", "TEXT DEFAULT \'\'"), ("grp_name", "TEXT DEFAULT \'\'"),\n'
    '                     ("ok", "INTEGER DEFAULT 1"), ("sender", "TEXT DEFAULT \'\'"), ("detail", "TEXT DEFAULT \'\'")):')

# 2) add_hit 助手
rep('''def get_setting(key, default=None):''',
'''def add_hit(action, ok, *, chat_id="", rule_id=0, msg_id=0, content="", reply="",
            grp_name="", sender="", detail=None):
    """统一写命中日志:kind(动作) + 成败 + 结构化详情(前端展开用)"""
    try:
        detail_json = json.dumps(detail or {}, ensure_ascii=False)[:2000]
    except Exception:
        detail_json = "{}"
    conn.execute(
        "INSERT INTO hits(ts,chat_id,rule_id,msg_id,action,content,reply,grp_name,ok,sender,detail) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (int(time.time()), str(chat_id), rule_id, msg_id, action, str(content)[:500],
         str(reply)[:500], str(grp_name)[:100], 1 if ok else 0, str(sender)[:80], detail_json),
    )
    conn.commit()


def get_setting(key, default=None):''')

# 3) 规则命中
rep('''        hits_count["total"] += 1
        conn.execute(
            "INSERT INTO hits(ts,chat_id,rule_id,msg_id,action,content,reply,grp_name) VALUES(?,?,?,?,?,?,?,?)",
            (int(time.time()), str(event.chat_id), rule["id"], event.id, action, text[:200], reply_desc[:200], grp_name),
        )
        conn.commit()''',
'''        hits_count["total"] += 1
        add_hit(action, True, chat_id=event.chat_id, rule_id=rule["id"], msg_id=event.id,
                content=text, reply=reply_desc, grp_name=grp_name, sender=sender_name,
                detail={"kind": "rule", "group": grp_name or str(event.chat_id), "sender": sender_name,
                        "message": text, "action": action, "reply": reply_desc,
                        "rule": rule["keywords"]})''')

# 4) 抢注 / 抢红包
rep('''            conn.execute(
                "INSERT INTO hits(ts,chat_id,rule_id,msg_id,action,content,reply,grp_name) VALUES(?,?,?,?,?,?,?,?)",
                (int(time.time()), chat, -a["id"], event.id, a["type"], text[:200], reply_desc[:200], grp_name),
            )
            conn.commit()''',
'''            add_hit(a["type"], True, chat_id=chat, rule_id=-a["id"], msg_id=event.id,
                    content=text, reply=reply_desc, grp_name=grp_name, sender=sender_name,
                    detail={"kind": a["type"], "task": a["name"], "group": grp_name or str(chat),
                            "sender": sender_name, "message": text, "reply": reply_desc,
                            "action": "回复消息" if a["action"] != "send" else "群内发送"})''')

# 5) 签到
rep('''    conn.execute(
        "INSERT INTO hits(ts,chat_id,rule_id,msg_id,action,content,reply,grp_name) VALUES(?,?,?,?,?,?,?,?)",
        (int(time.time()), target, -a["id"], 0, "signin", cmd[:200], desc[:200], target),
    )
    conn.commit()''',
'''    add_hit("signin", not (fail or ok is False), chat_id=target, rule_id=-a["id"], grp_name=a["name"],
            content=cmd, reply=reply_txt or desc, sender="",
            detail={"kind": "signin", "task": a["name"], "bot": target, "command": cmd,
                    "reply": reply_txt, "button": btn_text_clicked, "captcha": cap_ans,
                    "points": points, "note": desc})''')

# 6) 自定义任务
rep('''    conn.execute(
        "INSERT INTO hits(ts,chat_id,rule_id,msg_id,action,content,reply,grp_name) VALUES(?,?,?,?,?,?,?,?)",
        (int(time.time()), target, -a["id"], 0, "custom", f"自定义任务: {a['name']}"[:200], hits_desc, target),
    )
    conn.commit()''',
'''    add_hit("custom", final_ok, chat_id=target, rule_id=-a["id"], grp_name=a["name"],
            content=f"自定义任务: {a['name']}", reply=final_desc,
            detail={"kind": "custom", "task": a["name"], "bot": target, "steps": final_desc,
                    "ok": final_ok})''')

# 7) 轮询抢注命中
rep('''                                conn.execute(
                                    "INSERT INTO hits(ts,chat_id,rule_id,msg_id,action,content,reply,grp_name) VALUES(?,?,?,?,?,?,?,?)",
                                    (int(time.time()), a["target"], -a["id"], 0, "grabcodes",
                                     f"轮询{probe} 命中开注"[:200], f"注册: {reg}"[:200], grp_name),
                                )
                                conn.commit()''',
'''                                add_hit("grabcodes", True, chat_id=a["target"], rule_id=-a["id"], grp_name=a["name"],
                                        content=f"轮询 {probe} 命中开注", reply=rt,
                                        detail={"kind": "grabcodes", "task": a["name"], "bot": a["target"],
                                                "probe": probe, "reply": rt, "registered": reg})''')

# 8) 签到记录点击的按钮文字
rep('''    clicked_btn = False
    if a["click_btn"]:''',
'''    clicked_btn = False
    btn_text_clicked = ""
    if a["click_btn"]:''')

rep('''                await b.click()
                clicked_btn = True''',
'''                await b.click()
                clicked_btn = True
                btn_text_clicked = getattr(b, "text", "") or ""''')

# 9) 取发送者名字(规则命中 / 自动化共用)
rep('''async def on_group_message(event):''',
'''async def sender_name_of(event):
    """尽量取消息发送者显示名,失败返回空串"""
    try:
        sender = await event.get_sender()
        if sender is None:
            return ""
        nm = getattr(sender, "first_name", "") or ""
        ln = getattr(sender, "last_name", "") or ""
        return (nm + (" " + ln if ln else "")).strip() or getattr(sender, "username", "") or ""
    except Exception:
        return ""


async def on_group_message(event):''')

# 规则命中处取 sender
rep('''    if get_setting("ignore_bots", "1") == "1":
        try:
            sender = await event.get_sender()
            if getattr(sender, "bot", False):
                return
        except Exception:
            pass''',
'''    sender_name = ""
    try:
        sender = await event.get_sender()
        if get_setting("ignore_bots", "1") == "1" and getattr(sender, "bot", False):
            return
        nm = (getattr(sender, "first_name", "") or "") + ((" " + sender.last_name) if getattr(sender, "last_name", None) else "")
        sender_name = nm.strip() or getattr(sender, "username", "") or ""
    except Exception:
        pass''')

# 自动化事件处取 sender
rep('''    try:
            grp_name = ""''', '''    try:
            sender_name = await sender_name_of(event)
            grp_name = ""''') if False else None

open(P, 'w').write(s)
print('backend patch3 ok, replacements =', n)
