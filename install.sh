#!/usr/bin/env bash
# ============================================================
#  TG Persona · 询问式一键部署脚本
#  运行:  ./install.sh
#  会依次询问: ① 监听端口  ② 面板登录密码(回车自动生成)
#  自动完成: 依赖安装 → systemd 服务注册 → 启动
# ============================================================
set -e

C_GREEN='\033[0;32m'; C_CYAN='\033[0;36m'; C_YELLOW='\033[1;33m'; C_RED='\033[0;31m'; C_NC='\033[0m'
ok()  { echo -e "${C_GREEN}[OK]${C_NC} $1"; }
warn(){ echo -e "${C_YELLOW}[!]${C_NC} $1"; }
err() { echo -e "${C_RED}[X]${C_NC} $1"; exit 1; }
ask() { echo -e "${C_CYAN}?>${C_NC} $1"; }

# ---------- 0. 检查运行环境 ----------
[ "$(id -u)" -eq 0 ] || err "请用 root 运行:  sudo ./install.sh"
command -v python3 >/dev/null 2>&1 || err "未检测到 python3,请先:  apt install -y python3 python3-venv"
command -v systemctl >/dev/null 2>&1 || err "未检测到 systemd,本脚本仅支持 systemd 系统"
command -v curl >/dev/null 2>&1 || { warn "未检测到 curl,尝试安装..."; apt update -qq && apt install -y -qq curl; }

# 默认安装目录(当前脚本所在目录的上级,即仓库根目录)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_DIR="/opt/tg-persona"

# ---------- 1. 询问安装目录 ----------
ask "安装到哪个目录? [回车默认 ${DEFAULT_DIR}]"
read -r -p "> 目录: " INSTALL_DIR
INSTALL_DIR="${INSTALL_DIR:-$DEFAULT_DIR}"
INSTALL_DIR="${INSTALL_DIR%/}"
mkdir -p "$INSTALL_DIR" || err "无法创建目录 $INSTALL_DIR"

# 若仓库文件不在目标目录,则从当前目录复制
if [ "$SCRIPT_DIR" != "$INSTALL_DIR" ]; then
    if [ -f "$SCRIPT_DIR/main.py" ]; then
        cp -r "$SCRIPT_DIR"/. "$INSTALL_DIR"/ 2>/dev/null || true
    fi
fi
[ -f "$INSTALL_DIR/main.py" ] || err "未在 $INSTALL_DIR 找到 main.py,请确认已 clone 仓库且在本目录运行"

# ---------- 2. 询问端口 ----------
ask "TG Persona 面板监听端口?"
read -r -p "> 端口 [默认 8090]: " PORT
PORT="${PORT:-8090}"
case "$PORT" in
    ''|*[!0-9]*) err "端口必须是数字(1-65535)" ;;
    *) [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || err "端口范围 1-65535" ;;
esac

# ---------- 3. 询问面板密码 ----------
ask "设置面板登录密码(至少 6 位,留空自动生成随机密码)"
read -r -s -p "> 密码(输入时不显示): " PANEL_PW
echo ""
if [ -z "$PANEL_PW" ]; then
    PANEL_PW=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 12)
    warn "已自动生成面板密码,安装完成后请记下"
fi
[ "${#PANEL_PW}" -ge 6 ] || err "面板密码至少 6 位"

# ---------- 4. 安装 Python 依赖 ----------
ask "安装 Python 依赖(telethon / fastapi / uvicorn)..."
if ! python3 -m venv "$INSTALL_DIR/venv" 2>/dev/null; then
    warn "python3-venv 缺失,尝试安装..."
    apt update -qq && apt install -y -qq python3-venv
    python3 -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/pip" install -q --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"
ok "依赖安装完成"

# ---------- 5. 生成 systemd 服务 ----------
SERVICE_FILE="/etc/systemd/system/tg-persona.service"
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=TG Persona - Telegram 人形分身监听
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
Environment=PANEL_PASSWORD=$PANEL_PW
ExecStart=$INSTALL_DIR/venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port $PORT
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable -q tg-persona
systemctl restart tg-persona
sleep 2
ok "服务已启动"

# ---------- 6. 检查状态 ----------
if systemctl is-active -q tg-persona; then
    ok "TG Persona 运行正常"
else
    err "服务启动失败,请查看日志:  journalctl -u tg-persona -n 50"
fi

# ---------- 7. 输出访问信息 ----------
LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "你的服务器IP")
echo ""
echo "=============================================================="
echo "  TG Persona 部署完成"
echo ""
echo "  访问地址:  http://$LOCAL_IP:$PORT"
echo "  面板密码:  $PANEL_PW"
echo ""
echo "  常用命令:"
echo "    查看状态:  systemctl status tg-persona"
echo "    实时日志:  journalctl -u tg-persona -f"
echo "    停止服务:  systemctl stop tg-persona"
echo "    修改密码:  编辑 $SERVICE_FILE 里的 PANEL_PASSWORD 后"
echo "               systemctl daemon-reload && systemctl restart tg-persona"
echo "=============================================================="
