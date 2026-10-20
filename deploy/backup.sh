#!/usr/bin/env bash
# Резервная копия базы: дамп на диск с ротацией и копия в Telegram.
#
# Запуск из корня проекта: ./deploy/backup.sh
# В cron: 0 3 * * * cd /opt/elizabet-magic && ./deploy/backup.sh >> /var/log/em-backup.log 2>&1
#
# Копия уезжает в Telegram, потому что копия на том же диске пропадёт вместе
# с сервером. Куда слать — BACKUP_CHAT_ID, по умолчанию OWNER_CHAT_ID (заказчица).
# Служебный чат сборщика по умолчанию НЕ используется: в дампе персональные данные
# покупателей. Чтобы отключить отправку, поставьте BACKUP_CHAT_ID=0.
#
# Если бэкап не получился, в тот же чат уходит сообщение об ошибке — иначе о
# сломанных бэкапах узнали бы, только когда понадобилось бы восстанавливаться.
#
# Если задать BACKUP_PASSPHRASE, файл уедет зашифрованным.

set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/var/backups/elizabet-magic}"
KEEP_DAYS="${KEEP_DAYS:-14}"
STAMP="$(date +%Y-%m-%d_%H-%M-%S)"
FILE="${BACKUP_DIR}/em-${STAMP}.sql.gz"
# Дамп пишется во временный файл и получает настоящее имя, только когда готов:
# при сбое удаляется лишь недописанный файл этого запуска, а не прошлые копии.
PART="${FILE}.part"
TELEGRAM_API="${TELEGRAM_API:-https://api.telegram.org}"

# Значение из .env. Файл не выполняется через bash (`. ./.env`): пароли и секреты
# в нём могут содержать пробелы, $ или скобки — bash на них споткнулся бы, и бэкап
# молча не снялся бы. Берём строку KEY=… как есть, снимая кавычки по краям.
env_value() {
  local key="$1" line value
  [ -f .env ] || return 0
  line="$(grep -E "^[[:space:]]*${key}=" .env | tail -n 1)" || return 0
  value="${line#*=}"
  value="${value%$'\r'}"
  case "${value}" in
    \"*\") value="${value:1:${#value}-2}" ;;
    \'*\') value="${value:1:${#value}-2}" ;;
  esac
  printf '%s' "${value}"
}

# Переменные окружения важнее .env — так удобно проверять скрипт локально.
DB_USER="${POSTGRES_USER:-$(env_value POSTGRES_USER)}"
DB_USER="${DB_USER:-em}"
DB_NAME="${POSTGRES_DB:-$(env_value POSTGRES_DB)}"
DB_NAME="${DB_NAME:-elizabet_magic}"
BOT_TOKEN="${BOT_TOKEN:-$(env_value BOT_TOKEN)}"
CHAT_ID="${BACKUP_CHAT_ID:-$(env_value BACKUP_CHAT_ID)}"
CHAT_ID="${CHAT_ID:-${OWNER_CHAT_ID:-$(env_value OWNER_CHAT_ID)}}"
PASSPHRASE="${BACKUP_PASSPHRASE:-$(env_value BACKUP_PASSPHRASE)}"
# Команда дампа вынесена в переменную: на сервере база в контейнере, а при
# проверке скрипта локально удобно подставить обычный pg_dump.
PG_DUMP="${PG_DUMP:-docker compose exec -T postgres pg_dump}"

telegram_enabled() {
  [ -n "${BOT_TOKEN}" ] && [ -n "${CHAT_ID}" ] && [ "${CHAT_ID}" != "0" ]
}

# Сообщение об ошибке — в тот же чат, куда приходят копии.
alert() {
  echo "$1" >&2
  telegram_enabled || return 0
  curl -sS --max-time 30 \
    --data-urlencode "chat_id=${CHAT_ID}" \
    --data-urlencode "text=⚠️ Бэкап базы $(hostname) не получился: $1" \
    "${TELEGRAM_API}/bot${BOT_TOKEN}/sendMessage" > /dev/null || true
}

# Любая непредвиденная ошибка: убрать недописанный файл и сообщить.
on_error() {
  rm -f "${PART}" "${FILE}.enc"
  alert "сбой на строке $1 скрипта backup.sh, подробности — в /var/log/em-backup.log"
}
trap 'on_error $LINENO' ERR

mkdir -p "${BACKUP_DIR}"

echo "[$(date '+%F %H:%M')] Снимаю дамп ${DB_NAME}…"
${PG_DUMP} -U "${DB_USER}" -d "${DB_NAME}" | gzip -9 > "${PART}"

# Пустой или подозрительно маленький дамп — это не бэкап.
SIZE="$(stat -c %s "${PART}" 2>/dev/null || stat -f %z "${PART}")"
if [ "${SIZE}" -lt 1024 ]; then
  trap - ERR
  rm -f "${PART}"
  alert "дамп получился слишком маленьким (${SIZE} байт) — проверьте базу"
  exit 1
fi

mv "${PART}" "${FILE}"
echo "[$(date '+%F %H:%M')] Готово: ${FILE} ($((SIZE / 1024)) КБ)"

# --- копия в Telegram ---

send_to_telegram() {
  local file="$1"
  local caption="$2"

  if ! telegram_enabled; then
    echo "Отправка в Telegram выключена: нет BOT_TOKEN или BACKUP_CHAT_ID/OWNER_CHAT_ID" >&2
    return 0
  fi

  # Бот не отправит файл больше 50 МБ. До этого объёма базе очень далеко,
  # но если дорастём — копию нужно будет возить в хранилище, а не в чат.
  local max_bytes=$((50 * 1024 * 1024))
  local size
  size="$(stat -c %s "${file}" 2>/dev/null || stat -f %z "${file}")"
  if [ "${size}" -gt "${max_bytes}" ]; then
    alert "файл ${size} байт — больше лимита Telegram, копия осталась только на диске"
    return 1
  fi

  local response
  response="$(curl -sS --max-time 120 \
    -F "chat_id=${CHAT_ID}" \
    -F "document=@${file}" \
    -F "caption=${caption}" \
    "${TELEGRAM_API}/bot${BOT_TOKEN}/sendDocument")" || {
    echo "Не удалось связаться с Telegram — копия осталась только на диске" >&2
    return 1
  }

  case "${response}" in
    *'"ok":true'*) echo "[$(date '+%F %H:%M')] Копия отправлена в Telegram" ;;
    *)
      echo "Telegram не принял файл: ${response}" >&2
      return 1
      ;;
  esac
}

SEND_FILE="${FILE}"
CAPTION="Бэкап базы ${DB_NAME} · ${STAMP} · $((SIZE / 1024)) КБ"

if [ -n "${PASSPHRASE}" ]; then
  # Шифруем перед отправкой: в дампе персональные данные покупателей.
  # Расшифровать: openssl enc -d -aes-256-cbc -pbkdf2 -in файл.enc -out файл.sql.gz
  SEND_FILE="${FILE}.enc"
  BACKUP_PASSPHRASE="${PASSPHRASE}" openssl enc -aes-256-cbc -pbkdf2 -salt \
    -pass env:BACKUP_PASSPHRASE -in "${FILE}" -out "${SEND_FILE}"
  CAPTION="${CAPTION} · зашифрован"
fi

# Неудачная отправка не должна ронять сам бэкап: дамп на диске уже есть.
send_to_telegram "${SEND_FILE}" "${CAPTION}" || true
[ "${SEND_FILE}" = "${FILE}" ] || rm -f "${SEND_FILE}"

# Ротация: старые копии удаляем, чтобы диск не кончился.
find "${BACKUP_DIR}" -name 'em-*.sql.gz' -mtime "+${KEEP_DAYS}" -delete
# Недописанные файлы от оборванных запусков (сервер перезагрузился посреди дампа).
find "${BACKUP_DIR}" -name 'em-*.part' -mmin +60 -delete
echo "Копий на диске: $(find "${BACKUP_DIR}" -name 'em-*.sql.gz' | wc -l)"
