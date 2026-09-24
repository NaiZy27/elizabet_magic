#!/usr/bin/env bash
# Резервная копия базы: дамп на диск с ротацией и копия в Telegram.
#
# Запуск из корня проекта: ./deploy/backup.sh
# В cron: 0 3 * * * cd /opt/elizabet-magic && ./deploy/backup.sh >> /var/log/em-backup.log 2>&1
#
# Копия уезжает в Telegram, потому что копия на том же диске пропадёт вместе
# с сервером. Куда слать — BACKUP_CHAT_ID (по умолчанию SERVICE_CHAT_ID, иначе
# OWNER_CHAT_ID). Чтобы отключить отправку, поставьте BACKUP_CHAT_ID=0.
#
# В дампе есть персональные данные покупателей (имена, телефоны, email), поэтому
# чат должен быть закрытым. Если задать BACKUP_PASSPHRASE, файл уедет зашифрованным.

set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/var/backups/elizabet-magic}"
KEEP_DAYS="${KEEP_DAYS:-14}"
STAMP="$(date +%Y-%m-%d_%H-%M)"
FILE="${BACKUP_DIR}/em-${STAMP}.sql.gz"

# Пароль и имя базы берём из .env, чтобы не дублировать их в двух местах.
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

DB_USER="${POSTGRES_USER:-em}"
DB_NAME="${POSTGRES_DB:-elizabet_magic}"
# Команда дампа вынесена в переменную: на сервере база в контейнере, а при
# проверке скрипта локально удобно подставить обычный pg_dump.
PG_DUMP="${PG_DUMP:-docker compose exec -T postgres pg_dump}"
CHAT_ID="${BACKUP_CHAT_ID:-${SERVICE_CHAT_ID:-${OWNER_CHAT_ID:-}}}"
BOT_TOKEN="${BOT_TOKEN:-}"

mkdir -p "${BACKUP_DIR}"

echo "[$(date +%H:%M)] Снимаю дамп ${DB_NAME}…"
${PG_DUMP} -U "${DB_USER}" -d "${DB_NAME}" | gzip -9 > "${FILE}"

# Пустой или подозрительно маленький дамп — это не бэкап.
SIZE="$(stat -c %s "${FILE}" 2>/dev/null || stat -f %z "${FILE}")"
if [ "${SIZE}" -lt 1024 ]; then
  echo "Дамп получился слишком маленьким (${SIZE} байт) — проверьте базу" >&2
  rm -f "${FILE}"
  exit 1
fi

echo "[$(date +%H:%M)] Готово: ${FILE} ($((SIZE / 1024)) КБ)"

# --- копия в Telegram ---

send_to_telegram() {
  local file="$1"
  local caption="$2"

  if [ -z "${BOT_TOKEN}" ] || [ -z "${CHAT_ID}" ] || [ "${CHAT_ID}" = "0" ]; then
    echo "Отправка в Telegram выключена: нет BOT_TOKEN или BACKUP_CHAT_ID" >&2
    return 0
  fi

  # Бот не отправит файл больше 50 МБ. До этого объёма базе очень далеко,
  # но если дорастём — копию нужно будет возить в хранилище, а не в чат.
  local max_bytes=$((50 * 1024 * 1024))
  local size
  size="$(stat -c %s "${file}" 2>/dev/null || stat -f %z "${file}")"
  if [ "${size}" -gt "${max_bytes}" ]; then
    echo "Файл ${size} байт — больше лимита Telegram, копия осталась только на диске" >&2
    return 1
  fi

  local response
  response="$(curl -sS --max-time 120 \
    -F "chat_id=${CHAT_ID}" \
    -F "document=@${file}" \
    -F "caption=${caption}" \
    "https://api.telegram.org/bot${BOT_TOKEN}/sendDocument")" || {
    echo "Не удалось связаться с Telegram — копия осталась только на диске" >&2
    return 1
  }

  case "${response}" in
    *'"ok":true'*) echo "[$(date +%H:%M)] Копия отправлена в Telegram" ;;
    *)
      echo "Telegram не принял файл: ${response}" >&2
      return 1
      ;;
  esac
}

SEND_FILE="${FILE}"
CAPTION="Бэкап базы ${DB_NAME} · ${STAMP} · $((SIZE / 1024)) КБ"

if [ -n "${BACKUP_PASSPHRASE:-}" ]; then
  # Шифруем перед отправкой: в дампе персональные данные покупателей.
  # Расшифровать: openssl enc -d -aes-256-cbc -pbkdf2 -in файл.enc -out файл.sql.gz
  SEND_FILE="${FILE}.enc"
  openssl enc -aes-256-cbc -pbkdf2 -salt -pass env:BACKUP_PASSPHRASE -in "${FILE}" -out "${SEND_FILE}"
  CAPTION="${CAPTION} · зашифрован"
fi

# Неудачная отправка не должна ронять сам бэкап: дамп на диске уже есть.
send_to_telegram "${SEND_FILE}" "${CAPTION}" || true
[ "${SEND_FILE}" = "${FILE}" ] || rm -f "${SEND_FILE}"

# Ротация: старые копии удаляем, чтобы диск не кончился.
find "${BACKUP_DIR}" -name 'em-*.sql.gz' -mtime "+${KEEP_DAYS}" -delete
echo "Копий на диске: $(find "${BACKUP_DIR}" -name 'em-*.sql.gz' | wc -l)"
