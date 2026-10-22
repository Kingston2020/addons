#!/usr/bin/env bash
# Install agent-graph on an Ubuntu/Debian VPS. Run as root from the agent-graph directory.
set -euo pipefail

APP=/opt/agent-graph
SRC="$APP/app/agent-graph"

timedatectl set-timezone America/Jamaica
apt-get update -q
apt-get install -y -q git python3 python3-venv
id agentgraph >/dev/null 2>&1 || useradd --system --create-home --home-dir "$APP" --shell /usr/sbin/nologin agentgraph

mkdir -p "$APP/app"
if [ ! -d "$APP/app/.git" ]; then
  git clone --quiet --branch "${BRANCH:-master}" https://github.com/kingston2020/addons.git "$APP/app"
fi
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q -r "$SRC/requirements.txt"

ENVF="$APP/app/agent-graph/.env"
if [ ! -f "$ENVF" ]; then
  cp "$APP/app/agent-graph/.env.example" "$ENVF"
  chown -R agentgraph:agentgraph "$APP"
  chmod 600 "$ENVF"
  echo "Fill in $ENVF, then rerun this script."
  exit 0
fi
chown -R agentgraph:agentgraph "$APP"
chmod 600 "$ENVF"

install -m 644 "$SRC"/deploy/agent-graph*.service "$SRC"/deploy/agent-graph*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now agent-graph.service agent-graph-nightly.timer agent-graph-report.timer
systemctl --no-pager status agent-graph.service | head -5
