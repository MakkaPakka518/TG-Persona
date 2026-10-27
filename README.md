# TG Persona · Telegram 人形分身

部署在小型 VPS 上的个人账号自动回复工具。**不是机器人**——登录你自己的 Telegram 账号,监听的群里有人发你设定的关键词时,以你的身份回复文本 / 点表情。全部通过 **Web 面板可视化管理**(http://IP:端口)。

## 功能

- **可视化 TG 登录**:面板里填 API_ID / API_HASH → 手机号 → 验证码 → (两步验证密码),会话持久保存,重启免登录;登录后自动同步头像
- **面板安全**:密码用 scrypt 哈希存储(不落明文、不写进 systemd unit);登录成功发随机会话令牌(30 天,可"退出所有设备");连续输错 3 次才要求图形验证码,10 次后自动锁定 10 分钟
- **群管理**:列出账号加入的所有群(可搜索),一键加入 / 取消监听
- **关键词规则**:
  - 关键词多词逗号分隔(任意命中)、支持正则
  - 动作三选一:回复文本 / 点表情 / 回复+表情
  - 回复内容多条用 `||` 分隔,随机选一条,更像真人
  - **生效群**:每条规则可单独勾选只在哪几个群生效(不勾 = 全部监听群)
- **自动化任务**(「自动化」页,四类):
  - **签到**:每天在设定时间窗口内随机时刻执行;命令支持 `|` 多条随机、`{num:N}` `{word:N}` `{alpha:N}` `{uuid}` 随机占位符;可自动点回复按钮、成功/失败关键词判定、积分正则提取、发送失败重试(0-5)、发到群组
    - **按表情找按钮**:有些 bot 按钮文字每次都变(「好运来临」/「命中注定」)但图标不变,填表情(如 🧧)即可稳定命中
    - **连点**:部分 bot 需要连点多次才生效,可设「连点次数 1-20 + 间隔 50-3000ms」,每次点击前重新抓取最新按钮
  - **抢注监控**:监听群内邀请码/开注消息,自动回复或群内发送注册文本(`{code}` 占位符);**轮询模式**可选:每隔 N 秒主动探测机器人,出现开注关键词立刻注册
  - **抢红包**:群里出现红包/🎁消息自动点领取按钮
  - **自定义多步任务**:JSON 步骤流(发消息 / 等待 / 点按钮(任意、AI 选按钮)/ 识别并提交验证码 / 等回复匹配),整条失败可链级重试
- **验证码识别**:自定义任务里的「验证码」步骤优先调用 OpenAI 兼容视觉 API,未配置则用服务器本地 OCR(ddddocr,安装脚本自动装)
- **群保号(定时发言)**:每隔自定义时间(分钟/小时/天)在指定群发一条内容(支持 `||` 多条随机与占位符),可设置发送后 N 秒自动删除(Emby 保号常用);有「执行」按钮可立即发一条
- **通知推送**:签到成功/失败、抢注命中、自定义任务结束可选推送到 Bark 或 Telegram Bot(成功/失败分别开关)
- **自动水群(从众)**:默认关闭;开启后,监听群里同一句话短时间(默认 5 分钟)出现多人(默认 5 条)时,你也随机跟一条话术,像真人融入群聊;命令类消息不跟
- **时区设置**:面板里选时区(默认 `Asia/Shanghai`),签到窗口与"今天已签"都按它计算;VPS 是 UTC 也不用改系统时区
- **拟人化参数**:命中后随机延迟(默认 1~3 秒)、同群同规则冷却(默认 20 秒)、忽略机器人消息
- **主题与外观**:亮色 / 暗色主题一键切换(跨设备同步)、自定义头像、移动端友好 UI
- **日志管理**:默认保留 30 天自动清理(设置里可改,0=永久),支持按「清理 30 天前 / 清空全部」手动删除;支持按群名/发送人/消息内容搜索
- **会话守护**:每分钟检查一次 TG 登录状态,掉线立刻推送提醒(只推一次,恢复后自动复位)
- **多条规则同时命中**:一条消息可以同时触发多条规则(每条各自冷却),不再只执行第一条
- **运行面板**:总览统计、日志按类型分开(回复/签到/抢注/红包/自定义)、点一行展开完整记录(谁在哪个群发了什么、我回复/点了什么)、暂停/恢复开关、任务复制、面板密码修改

## 目录结构

```
tg-persona/
├── main.py            # FastAPI 后端 + Telethon 监听逻辑(全部核心)
├── install.sh         # 询问式一键部署脚本(端口 + 面板密码)
├── web/index.html     # 可视化面板前端(单文件,无外部依赖)
├── requirements.txt
├── tg-persona.service # systemd 服务(手动部署用,install.sh 会按询问结果自动生成)
├── data/              # 运行时自动创建:config.db / tg.session / panel_password.txt
└── README.md
```

## 安全提示(重要)

面板默认监听 `0.0.0.0`,直接暴露在公网时请至少做一件事:

```bash
# 方案 A:只监听本机,用 SSH 隧道访问(最简单)
sed -i 's/--host 0.0.0.0/--host 127.0.0.1/' /etc/systemd/system/tg-persona.service
systemctl daemon-reload && systemctl restart tg-persona
# 本地:ssh -L 8090:127.0.0.1:8090 root@你的服务器 → http://127.0.0.1:8090

# 方案 B:nginx 反代 + Let's Encrypt HTTPS
# 方案 C:至少用 ufw 只放行你自己的 IP
```

面板密码只有在**首次启动**时来自 `data/panel_password.txt` 或 `PANEL_PASSWORD` 环境变量,
之后会转成 scrypt 哈希存入 `data/config.db`,明文文件被自动删除。忘记密码时:

```bash
systemctl stop tg-persona
sqlite3 /opt/tg-persona/data/config.db "delete from settings where key='panel_pw_hash'"
PANEL_PASSWORD=新密码 /opt/tg-persona/venv/bin/python -m uvicorn main:app --port 8090   # 启动一次写回哈希
systemctl start tg-persona
```

## 部署(VPS,以 Debian/Ubuntu 为例)

**一键询问式安装(推荐)** — 脚本会询问你要用的端口和面板密码:

```bash
# 1. 拉取仓库
git clone https://github.com/MakkaPakka518/TG-Persona.git /opt/tg-persona
cd /opt/tg-persona

# 2. 运行询问式安装脚本(root 执行)
sudo ./install.sh
```

脚本会依次询问:
- **监听端口**(默认 8090)
- **面板登录密码**(回车自动生成随机密码)

随后自动完成:创建 venv → 安装依赖 → 注册 systemd 服务 → 启动,最后打印访问地址和密码。

**手动部署**(想自己控制每一步时):

```bash
# 1. 上传项目到服务器
#    scp -r tg-persona root@你的VPS:/opt/
#    (或 git clone https://github.com/MakkaPakka518/TG-Persona.git /opt/tg-persona)

# 2. 安装依赖
sudo apt update && sudo apt install -y python3 python3-venv
cd /opt/tg-persona
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# 3. 配置 systemd 服务(可先编辑端口和密码)
sudo cp tg-persona.service /etc/systemd/system/
#   编辑 service:改 --port 和 PANEL_PASSWORD 环境变量
sudo systemctl daemon-reload
sudo systemctl enable --now tg-persona

# 4. 查看启动状态与面板密码(仅当未设置 PANEL_PASSWORD 时)
sudo systemctl status tg-persona
cat /opt/tg-persona/data/panel_password.txt   # 首次启动自动生成的密码
```

之后浏览器打开 `http://你的VPS的IP:端口`,输入面板密码进入。

## 使用流程

1. 打开面板 → 输入面板密码 + 图形验证码进入
2. 进入「设置 → Telegram 账号」:**第 1 步**填 API_ID / API_HASH —— 去 [my.telegram.org](https://my.telegram.org) → API development tools → 登录后创建应用获取
3. **第 2 步**:填手机号(带国家码,如 `8613800138000`)→ 发送验证码
4. **第 3 步**:填 TG 发来的验证码;若账号开了两步验证,再填 Cloud 密码
5. 进入「群组」,搜索并加入要监听的群
6. 进入「规则」,添加关键词规则(如关键词 `求推荐,有没有` → 回复文本 `好的我看看||来了来了` 或点 `👍`)
7. 进入「自动化」,按需添加签到 / 抢注 / 抢红包 / 自定义任务(任务支持一键复制)
8. 完成。群里有人命中关键词,你的账号会自动回复/点表情;自动化任务会按计划执行并在「日志」留痕

## 安全说明

- Telegram 账号会话保存在 `data/tg.session`,目录权限 700 / 文件 600
- 面板密码存 `data/panel_password.txt`(600),面板所有 API 均需密码鉴权(cookie)
- 不建议把面板端口(8090)直接暴露公网;可用防火墙限定 IP,或通过 Nginx 反代加 HTTPS
- 提示:频繁回复可能触发 Telegram 限流(代码会自动等待 FloodWait),请合理设置冷却时间

## 常见问题

- **收不到验证码**:确认手机号带国家码;Telegram 要求号码前不要加 `+`
- **登录后群列表为空**:确认登录的是你自己加入了群的账号
- **改了端口**:编辑 service 里 `--port` 或设环境变量 `PORT`,然后 `sudo systemctl daemon-reload && sudo systemctl restart tg-persona`
- **日志怎么调大**:`sudo journalctl -u tg-persona -f` 实时查看
