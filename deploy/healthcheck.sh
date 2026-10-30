#!/usr/bin/env bash
# Минимальный мониторинг: раз в 5 минут проверить, что всё живо, и написать в Telegram,
# если нет. Работает на хосте, а не в контейнерах — поэтому сообщит и о том, что упал
# сам воркер (уведомления приложения ходят через него).
#
# Запуск из корня проекта: ./deploy/healthcheck.sh
# В cron: */5 * * * * cd /opt/elizabet-magic && ./deploy/healthcheck.sh >> /var/log/em-health.log 2>&1
#
# Что проверяется:
# - контейнеры postgres, redis, web, bot, worker, scheduler запущены, а у кого есть
#   healthcheck — здоровы (у worker это заодно проверка планировщика, см. core/health.py);
# - сообщения клиентам не застряли в очереди outbox дольше 15 минут и не копятся
#   недоставленные;
# - на диске больше 10% свободного места;
# - есть свежий бэкап базы (не старше 26 часов).
#
# Пишет только при изменении: «⚠️ проблема» — когда появилась, «✅ всё в порядке» —
# когда ушла. Куда — ALERT_CHAT_ID, по умолчанию OWNER_CHAT_ID.

set -uo pipefail

SERVICES="postgres redis web bot worker scheduler"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/elizabet-magic}"
BACKUP_MAX_AGE_MIN="${BACKUP_MAX_AGE_MIN:-1560}"
DISK_MAX_PERCENT="${DISK_MAX_PERCENT:-90}"
STATE_FILE="${STATE_FILE:-/var/tmp/em-health.state}"
TELEGRAM_API="${TELEGRAM_API:-https://api.telegram.org}"

# Значение из .env — так же, как в backup.sh: файл не выполняется через bash.
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

DB_USER="${POSTGRES_USER:-$(env_value POSTGRES_USER)}"
DB_USER="${DB_USER:-em}"
DB_NAME="${POSTGRES_DB:-$(env_value POSTGRES_DB)}"
DB_NAME="${DB_NAME:-elizabet_magic}"
BOT_TOKEN="${BOT_TOKEN:-$(env_value BOT_TOKEN)}"
CHAT_ID="${ALERT_CHAT_ID:-$(env_value ALERT_CHAT_ID)}"
CHAT_ID="${CHAT_ID:-${OWNER_CHAT_ID:-$(env_value OWNER_CHAT_ID)}}"

problems=()

# --- контейнеры ---
for service in ${SERVICES}; do
  status=""
  for id in $(docker compose ps -q "${service}" 2>/dev/null); do
    # Разовые контейнеры (`docker compose run`, например ручная загрузка Ozon) не в счёт.
    [ "$(docker inspect -f '{{index .Config.Labels "com.docker.compose.oneoff"}}' "${id}")" = "False" ] || continue
    status="$(docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' "${id}")"
  done
  case "${status}" in
    "") problems+=("${service}: контейнер не запущен") ;;
    "running "|"running healthy"|"running starting") ;;
    "running unhealthy")
      if [ "${service}" = "worker" ]; then
        problems+=("worker: нет пульса — завис воркер или планировщик (scheduler)")
      else
        problems+=("${service}: не отвечает на проверку здоровья")
      fi
      ;;
    *) problems+=("${service}: ${status}") ;;
  esac
done

# --- очередь сообщений ---
psql_value() {
  docker compose exec -T postgres psql -U "${DB_USER}" -d "${DB_NAME}" -tAc "$1" 2>/dev/null | tr -d '[:space:]'
}
stuck="$(psql_value "SELECT count(*) FROM outbox WHERE status = 'pending' AND created_at < now() - interval '15 minutes'")"
if [ -n "${stuck}" ] && [ "${stuck}" != "0" ]; then
  problems+=("сообщений в очереди дольше 15 минут: ${stuck}")
fi
failed="$(psql_value "SELECT count(*) FROM outbox WHERE status = 'failed' AND updated_at > now() - interval '1 day' AND kind <> 'customer'")"
if [ -n "${failed}" ] && [ "${failed}" != "0" ]; then
  # Клиентские сообщения не в счёт: клиент мог просто заблокировать бота.
  problems+=("не доставлено служебных сообщений за сутки: ${failed} (таблица outbox, last_error)")
fi

# --- диск ---
disk="$(df -P / | awk 'NR == 2 { gsub("%", "", $5); print $5 }')"
if [ -n "${disk}" ] && [ "${disk}" -ge "${DISK_MAX_PERCENT}" ]; then
  problems+=("диск занят на ${disk}%")
fi

# --- бэкап ---
if [ -d "${BACKUP_DIR}" ]; then
  if [ -z "$(find "${BACKUP_DIR}" -name 'em-*.sql.gz' -mmin "-${BACKUP_MAX_AGE_MIN}" | head -n 1)" ]; then
    problems+=("нет бэкапа базы за последние сутки — проверьте cron и /var/log/em-backup.log")
  fi
fi

# --- сообщить, если что-то изменилось ---
current="$(printf '%s\n' "${problems[@]+"${problems[@]}"}")"
previous="$(cat "${STATE_FILE}" 2>/dev/null || true)"
[ "${current}" = "${previous}" ] && exit 0

if [ -n "${current}" ]; then
  text="⚠️ $(hostname): проблемы с Elizabet Magic"$'\n'"$(printf '• %s\n' "${problems[@]}")"
else
  text="✅ $(hostname): Elizabet Magic снова в порядке"
fi
echo "[$(date '+%F %H:%M')] ${text}"

if [ -n "${BOT_TOKEN}" ] && [ -n "${CHAT_ID}" ] && [ "${CHAT_ID}" != "0" ]; then
  if ! curl -sS --max-time 30 \
    --data-urlencode "chat_id=${CHAT_ID}" \
    --data-urlencode "text=${text}" \
    "${TELEGRAM_API}/bot${BOT_TOKEN}/sendMessage" > /dev/null; then
    # Не удалось отправить — состояние не запоминаем, попробуем через 5 минут.
    exit 1
  fi
fi
printf '%s' "${current}" > "${STATE_FILE}"
