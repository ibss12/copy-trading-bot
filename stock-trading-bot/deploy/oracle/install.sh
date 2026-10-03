#!/usr/bin/env bash
# One-command install of the stock command center on an Ubuntu 22.04/24.04 server
# (made for Oracle Cloud "Always Free"; works on any Ubuntu VM). Safe to run again to update.
#
#   curl -fsSL https://raw.githubusercontent.com/ibss12/copy-trading-bot/master/stock-trading-bot/deploy/oracle/install.sh \
#     | sudo STOCKBOT_PASSWORD='pick-a-long-password' bash
#
# Settings (environment variables):
#   STOCKBOT_PASSWORD  command center password (required the first time; keeps the old one if unset later)
#   STOCKBOT_DOMAIN    web address; default <public-ip>.sslip.io (free, no domain needed)
#   STOCKBOT_REPO      git repo to install  (default: this project's GitHub repo)
#   STOCKBOT_BRANCH    branch               (default: master)
set -euo pipefail

REPO="${STOCKBOT_REPO:-https://github.com/ibss12/copy-trading-bot.git}"
BRANCH="${STOCKBOT_BRANCH:-master}"
APP_USER=stockbot
APP_HOME=/opt/stockbot
SRC="$APP_HOME/app"
BOT_DIR="$SRC/stock-trading-bot"
VENV="$APP_HOME/venv"
PORT=8000

log() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run this with sudo"
. /etc/os-release
[ "${ID:-}" = ubuntu ] || die "this script is for Ubuntu (found ${ID:-unknown})"
export DEBIAN_FRONTEND=noninteractive

log "Installing system packages"
apt-get update -y
apt-get install -y --no-install-recommends ca-certificates curl git gnupg sudo debian-keyring debian-archive-keyring \
  apt-transport-https iptables iptables-persistent netfilter-persistent

if [ "$(awk '/MemTotal/ {print $2}' /proc/meminfo)" -lt 2000000 ] && ! swapon --show | grep -q .; then
  log "Small machine: adding a 2 GB swap file"
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

log "Installing uv (Python manager)"
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi

log "Creating the $APP_USER user and getting the code"
id "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --home-dir "$APP_HOME" --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_HOME"
chown "$APP_USER:$APP_USER" "$APP_HOME"
as_app() { sudo -u "$APP_USER" -H env HOME="$APP_HOME" UV_PYTHON_INSTALL_DIR="$APP_HOME/python" UV_CACHE_DIR="$APP_HOME/.cache/uv" "$@"; }
if [ -d "$SRC/.git" ]; then
  as_app git -C "$SRC" fetch --depth 1 origin "$BRANCH"
  as_app git -C "$SRC" checkout -q -B "$BRANCH" FETCH_HEAD
else
  as_app git clone --depth 1 --branch "$BRANCH" "$REPO" "$SRC"
fi
as_app git -C "$SRC" branch --set-upstream-to="origin/$BRANCH" "$BRANCH" >/dev/null 2>&1 || true
[ -f "$BOT_DIR/requirements.txt" ] || die "stock-trading-bot/ not found in $REPO ($BRANCH)"

log "Installing Python 3.11 and the bot's packages (takes a few minutes)"
[ -x "$VENV/bin/python" ] || as_app uv venv --python 3.11 "$VENV"
as_app uv pip install --python "$VENV/bin/python" -r "$BOT_DIR/requirements.txt"

log "Working out the web address"
if [ -z "${STOCKBOT_DOMAIN:-}" ]; then
  IP="$(curl -fsS --max-time 10 https://api.ipify.org || curl -fsS --max-time 10 https://ifconfig.me || true)"
  [[ "$IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "couldn't find this server's public IP; set STOCKBOT_DOMAIN"
  STOCKBOT_DOMAIN="${IP//./-}.sslip.io"
fi
echo "Address: https://$STOCKBOT_DOMAIN"

log "Writing settings ($BOT_DIR/.env)"
ENV_FILE="$BOT_DIR/.env"
[ -f "$ENV_FILE" ] || install -m 600 -o "$APP_USER" -g "$APP_USER" "$BOT_DIR/.env.example" "$ENV_FILE"
set_env() {  # set_env KEY VALUE  (replaces or appends; VALUE may contain any character except newline)
  local tmp; tmp="$(mktemp)"
  grep -v "^$1=" "$ENV_FILE" > "$tmp" || true
  printf '%s=%s\n' "$1" "$2" >> "$tmp"
  install -m 600 -o "$APP_USER" -g "$APP_USER" "$tmp" "$ENV_FILE"
  rm -f "$tmp"
}
current_password="$(grep -E '^DASHBOARD_PASSWORD=.+' "$ENV_FILE" | head -1 | cut -d= -f2- || true)"
if [ -n "${STOCKBOT_PASSWORD:-}" ]; then
  [ "${#STOCKBOT_PASSWORD}" -ge 8 ] || die "STOCKBOT_PASSWORD must be at least 8 characters"
  set_env DASHBOARD_PASSWORD "$STOCKBOT_PASSWORD"
elif [ -z "$current_password" ]; then
  die "set STOCKBOT_PASSWORD (the password you'll use to open the command center)"
fi
set_env BROKER trading212
set_env PUBLIC_URL "https://$STOCKBOT_DOMAIN"
set_env STOCKBOT_SELF_UPDATE 1
grep -q '^ALPACA_API_KEY=your_paper_api_key' "$ENV_FILE" && set_env ALPACA_API_KEY "" && set_env ALPACA_API_SECRET ""
chown -R "$APP_USER:$APP_USER" "$APP_HOME"

log "Installing the command center service"
cat > /etc/systemd/system/stockbot.service <<UNIT
[Unit]
Description=Stock trading command center
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$BOT_DIR
Environment=PATH=$VENV/bin:/usr/local/bin:/usr/bin:/bin
Environment=HOME=$APP_HOME
Environment=UV_CACHE_DIR=$APP_HOME/.cache/uv
Environment=UV_PYTHON_INSTALL_DIR=$APP_HOME/python
ExecStart=$VENV/bin/python run_dashboard.py --host 127.0.0.1 --port $PORT --no-browser
Restart=always
RestartSec=5
TimeoutStopSec=30
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable stockbot.service
systemctl restart stockbot.service

log "Installing Caddy (automatic HTTPS)"
if ! command -v caddy >/dev/null; then
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y
  apt-get install -y caddy
fi
cat > /etc/caddy/Caddyfile <<CADDY
$STOCKBOT_DOMAIN {
	encode gzip
	reverse_proxy 127.0.0.1:$PORT {
		flush_interval -1
	}
}
CADDY
systemctl enable caddy
systemctl restart caddy

log "Opening ports 80 and 443 in the server firewall"
# Oracle's Ubuntu images end the INPUT chain with a REJECT rule, so insert before it.
for port in 80 443; do
  iptables -C INPUT -p tcp --dport "$port" -m state --state NEW -j ACCEPT 2>/dev/null && continue
  reject="$(iptables -L INPUT --line-numbers -n | awk '$2 == "REJECT" {print $1; exit}')"
  iptables -I INPUT "${reject:-1}" -p tcp --dport "$port" -m state --state NEW -j ACCEPT
done
netfilter-persistent save >/dev/null 2>&1 || true

log "Checking the command center started"
for _ in $(seq 1 60); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/login" || true)"
  [ "$code" = 200 ] || [ "$code" = 303 ] && break
  sleep 2
done
[ "$code" = 200 ] || [ "$code" = 303 ] || { journalctl -u stockbot -n 40 --no-pager; die "the command center didn't start (see log above)"; }

cat <<DONE

==========================================================================
 Done. Open this on your phone or computer:

     https://$STOCKBOT_DOMAIN

 Sign in with your password, then add your Trading 212 practice account
 (account menu at the top) and tap the bell to turn on notifications.

 If the page doesn't load, open ports 80 and 443 in Oracle Cloud:
 Networking > Virtual cloud networks > your VCN > Security Lists >
 Default Security List > Add Ingress Rules (source 0.0.0.0/0, TCP 80 and 443).
==========================================================================
DONE
