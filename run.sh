#!/bin/bash
# run.sh — roda o Monitor OLX em primeiro plano (o serviço systemd e o Docker fazem o mesmo)
set -euo pipefail
cd "$(dirname "$0")"
[ -d venv ] || { echo "❌ Execute ./setup.sh primeiro."; exit 1; }
set -a; . ./.env; set +a          # aceita valores com espaço
exec ./venv/bin/python main.py
