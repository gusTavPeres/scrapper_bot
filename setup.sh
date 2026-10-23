#!/bin/bash
# setup.sh — instala/atualiza o Monitor OLX (Linux/macOS). Pode rodar de novo: não apaga o .env.
#   ./setup.sh             venv + dependências + .env + testes
#   ./setup.sh --servico   idem + serviço systemd (bot_web@$USER) e rotação de log (pede sudo)
set -euo pipefail
cd "$(dirname "$0")"

PY=${PYTHON:-python3}
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
  || { echo "❌ Precisa de Python 3.10+ (achei: $("$PY" --version 2>&1))"; exit 1; }

[ -d venv ] || "$PY" -m venv venv
./venv/bin/pip install --quiet --upgrade pip
./venv/bin/pip install --quiet -r requirements.txt
mkdir -p data logs

if [ ! -f .env ]; then
  umask 077
  sed "s|^BOT_PASS=.*|BOT_PASS=$(./venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(18))')|" \
    .env.example > .env
  echo "✅ .env criado (senha do painel gerada) — preencha TELEGRAM_TOKEN (e TELEGRAM_CHAT_ID)"
else
  echo "✅ .env existente mantido"
fi

if ./venv/bin/python test_offline.py >/dev/null 2>&1; then
  echo "✅ testes offline OK"
else
  echo "❌ testes falharam — veja: ./venv/bin/python test_offline.py"; exit 1
fi

if [ "${1:-}" = "--servico" ]; then      # serviço systemd + rotação de log (pede sudo)
  preenche() { sed -e "s|__DIR__|$PWD|g" -e "s|__USER__|$USER|g" "$1"; }
  preenche bot_web@.service | sudo tee /etc/systemd/system/bot_web@.service >/dev/null
  preenche logrotate.conf | sudo tee /etc/logrotate.d/scrapper_bot >/dev/null
  sudo systemctl daemon-reload
  sudo systemctl enable --now "bot_web@$USER"
  sudo systemctl restart "bot_web@$USER"
  echo "✅ serviço bot_web@$USER ativo — painel em http://localhost:$(sed -n 's/^PORT=//p' .env | grep . || echo 8080)"
else
  echo "Rodar: ./run.sh   ·   como serviço: ./setup.sh --servico"
fi
