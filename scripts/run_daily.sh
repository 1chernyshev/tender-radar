#!/bin/zsh
# Ежедневный сбор контрактов за последние сутки.
# cron (VPN должен быть включён — сайт доступен из-под него):
#   15 9 * * * /Users/cherny_shevv/Projects/tender-radar/scripts/run_daily.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$ROOT/logs"
# Пауза: пока существует файл data/PAUSED, сбор не запускается.
if [[ -f "$ROOT/data/PAUSED" ]]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') сбор на паузе: $(cat "$ROOT/data/PAUSED")" >> "$ROOT/logs/paused.log"
  exit 0
fi

cd "$ROOT/src"
# caffeinate не дает Mac уснуть, пока идет сбор
caffeinate -i "$ROOT/.venv/bin/python" -W ignore -m tender_radar fetch --pages 2 --days 1 --cards --delay 5 --keep-raw \
  >> "$ROOT/logs/fetch_$(date +%Y-%m-%d).log" 2>&1

# Добор контактов поставщиков, собранных до исправления разбора колонок.
# Идет после основного сбора, начиная с самых крупных контрактов.
caffeinate -i "$ROOT/.venv/bin/python" -W ignore -m tender_radar backfill-contacts --limit 40 --delay 6 \
  >> "$ROOT/logs/fetch_$(date +%Y-%m-%d).log" 2>&1
