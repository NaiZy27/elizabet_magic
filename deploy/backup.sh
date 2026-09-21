#!/usr/bin/env bash
# Резервная копия базы с ротацией.
# Запуск из корня проекта: ./deploy/backup.sh
# В cron: 0 3 * * * cd /opt/elizabet-magic && ./deploy/backup.sh >> /var/log/em-backup.log 2>&1

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

mkdir -p "${BACKUP_DIR}"

echo "[$(date +%H:%M)] Снимаю дамп ${DB_NAME}…"
docker compose exec -T postgres pg_dump -U "${DB_USER}" -d "${DB_NAME}" | gzip -9 > "${FILE}"

# Пустой или подозрительно маленький дамп — это не бэкап.
SIZE="$(stat -c %s "${FILE}" 2>/dev/null || stat -f %z "${FILE}")"
if [ "${SIZE}" -lt 1024 ]; then
  echo "Дамп получился слишком маленьким (${SIZE} байт) — проверьте базу" >&2
  rm -f "${FILE}"
  exit 1
fi

echo "[$(date +%H:%M)] Готово: ${FILE} ($((SIZE / 1024)) КБ)"

# Ротация: старые копии удаляем, чтобы диск не кончился.
find "${BACKUP_DIR}" -name 'em-*.sql.gz' -mtime "+${KEEP_DAYS}" -delete
echo "Копий в хранилище: $(find "${BACKUP_DIR}" -name 'em-*.sql.gz' | wc -l)"

# Копию нужно увозить с сервера — иначе она пропадёт вместе с ним.
# Пример: rclone copy "${FILE}" remote:elizabet-magic/
