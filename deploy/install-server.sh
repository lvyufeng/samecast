#!/usr/bin/env bash
# 把网关装到 Linux 服务器（Debian/Ubuntu 系，systemd）
#   ./deploy/install-server.sh                  # 装到本机
#   ./deploy/install-server.sh root@192.168.1.10 # 装到远程机器（需要 ssh 免密）
set -euo pipefail

TARGET="${1:-}"
PREFIX=/opt/samecast
UNIT=/etc/systemd/system/samecast.service
SERVICE_USER=samecast

HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
cd "$HERE"

PY="$(command -v python3 || true)"

remote() { ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$TARGET" "$@"; }

if [ -n "$TARGET" ]; then
  echo "==> 部署到 $TARGET:$PREFIX"
  remote "mkdir -p $PREFIX && rm -rf $PREFIX/server $PREFIX/web"
  tar czf - server web config.example.json | remote "tar xzf - --warning=no-unknown-keyword -C $PREFIX"
  ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$TARGET" "cat > $UNIT" < deploy/samecast.service
  # 首次部署时生成 config.json（之后不再覆盖）
  remote "if [ ! -f $PREFIX/config.json ]; then cp $PREFIX/config.example.json $PREFIX/config.json; fi"
  remote "set -e
    id -u $SERVICE_USER >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin $SERVICE_USER
    mkdir -p $PREFIX/data
    chown -R $SERVICE_USER:$SERVICE_USER $PREFIX
    systemctl daemon-reload
    systemctl enable samecast >/dev/null
    systemctl restart samecast
    sleep 2
    systemctl is-active samecast"
  echo "==> 状态: $(remote 'systemctl is-active samecast')"
  exit 0
fi

echo "==> 本地安装到 $PREFIX"
[ "$(id -u)" = "0" ] || { echo "本地安装需要 root（sudo $0）"; exit 1; }
[ -n "$PY" ] || { echo "找不到 python3"; exit 1; }
mkdir -p "$PREFIX/data"
rm -rf "$PREFIX/server" "$PREFIX/web"
cp -R server web "$PREFIX/"
if [ ! -f "$PREFIX/config.json" ]; then
  cp config.example.json "$PREFIX/config.json"
  echo "    已生成 $PREFIX/config.json，按需修改 friendly_name / port"
fi
id -u "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
cp deploy/samecast.service "$UNIT"
chown -R "$SERVICE_USER:$SERVICE_USER" "$PREFIX"
systemctl daemon-reload
systemctl enable samecast >/dev/null
systemctl restart samecast
sleep 2
systemctl is-active --quiet samecast && echo "==> samecast 已启动，访问 http://$(hostname -I | awk '{print $1}'):8090/"
