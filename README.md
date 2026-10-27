# TG Persona · Telegram 人形分身

部署在小 VPS 上的个人账号自动回复工具。**不是机器人**——登录你自己的 Telegram 账号,监听的群里有人发你设定的关键词时,以你的身份回复文本 / 点表情。全部通过 **Web 面板可视化管理**(http://IP:端口)。

## 功能

- **可视化 TG 登录**:面板里填 API_ID / API_HASH → 手机号 → 验证码 → (两步验证密码),会话持久保存,重启免登录
- **群管理**:列出账号加入的所有群(可搜索),一键加入 / 取消监听
- **关键词规则**:
  - 关键词多词逗号分隔(任意命中)、支持正则
  - 动作三选一:回复文本 / 点表情 / 回复+表情
  - 回复内容多条用 `||` 分隔,随机选一条,更像真人
- **拟人化参数**:命中后随机延迟(默认 1~3 秒)、同群同规则冷却(默认 20 秒)、忽略机器人消息
- **运行面板**:总览统计、命中日志、暂停/恢复开关、面板密码修改

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

1. 打开面板 → 自动进入登录向导(若未登录)
2. **第 1 步**:填 API_ID / API_HASH —— 去 [my.telegram.org](https://my.telegram.org) → API development tools → 登录后创建应用获取
3. **第 2 步**:填手机号(带国家码,如 `8613800138000`)→ 发送验证码
4. **第 3 步**:填 TG 发来的验证码;若账号开了两步验证,再填 Cloud 密码
5. 进入「群组」,搜索并加入要监听的群
6. 进入「规则」,添加关键词规则(如关键词 `求推荐,有没有` → 回复文本 `好的我看看||来了来了` 或点 `👍`)
7. 完成。群里有人命中关键词,你的账号会自动回复/点表情

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
