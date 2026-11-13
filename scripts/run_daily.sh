#!/bin/zsh
# Ежедневный сбор контрактов за последние сутки.
# launchd, ежедневно в 9:15 (нужен доступ к ЕИС и Telegram):
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
DAILY_LOG="$ROOT/logs/fetch_$(date +%Y-%m-%d).log"
daily_status=0
if caffeinate -i "$ROOT/.venv/bin/python" -W ignore -m tender_radar fetch --pages 2 --days 1 --cards --delay 5 --keep-raw \
  >> "$DAILY_LOG" 2>&1; then

# Общий бюджет 20: до 5 известных карточек, затем добор контактов остатком.
  if ! caffeinate -i "$ROOT/.venv/bin/python" -W ignore -m tender_radar backfill-contacts --limit 20 --refresh-limit 5 --refresh-days 7 --delay 10 --keep-raw \
    >> "$DAILY_LOG" 2>&1; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') WARNING добор не завершен; отправляю дайджест по имеющейся базе" >> "$DAILY_LOG"
    daily_status=1
  fi
else
  echo "$(date '+%Y-%m-%d %H:%M:%S') WARNING сбор не завершен; добор пропущен, отправляю дайджест по имеющейся базе" >> "$DAILY_LOG"
  daily_status=1
fi

# Дайджест свежих лидов в Telegram. Идет после добора контактов, чтобы в сообщении
# были актуальные телефоны и почта. Уже отправленные лиды повторно не присылаются.
if ! caffeinate -i "$ROOT/.venv/bin/python" -W ignore -m tender_radar digest \
  >> "$DAILY_LOG" 2>&1; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') ERROR дайджест не доставлен" >> "$DAILY_LOG"
  daily_status=1
fi
exit "$daily_status"
