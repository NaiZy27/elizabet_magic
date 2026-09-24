#!/usr/bin/env bash
# Собрать конфиги nginx под свои домены.
#
# Запуск из корня проекта:
#   ./deploy/render-nginx.sh elizabet.example crm.elizabet.example
#
# Готовые файлы кладутся в deploy/nginx/out/ — оттуда их копируют
# в /etc/nginx/sites-available на сервере.

set -euo pipefail

PUBLIC_DOMAIN="${1:-}"
ADMIN_DOMAIN="${2:-}"

if [ -z "${PUBLIC_DOMAIN}" ] || [ -z "${ADMIN_DOMAIN}" ]; then
  echo "Укажите оба домена: ./deploy/render-nginx.sh ваш-домен crm.ваш-домен" >&2
  exit 1
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${HERE}/nginx/out"
mkdir -p "${OUT}"

sed "s/example\.ru/${PUBLIC_DOMAIN}/g" "${HERE}/nginx/pay.conf" > "${OUT}/${PUBLIC_DOMAIN}.conf"
# В конфиге админки домен записан как crm.example.ru — меняем его целиком.
sed "s/crm\.example\.ru/${ADMIN_DOMAIN}/g" "${HERE}/nginx/crm.conf" > "${OUT}/${ADMIN_DOMAIN}.conf"

echo "Готово:"
echo "  ${OUT}/${PUBLIC_DOMAIN}.conf  — страница оплаты и вебхуки"
echo "  ${OUT}/${ADMIN_DOMAIN}.conf   — админка"
echo
echo "Дальше на сервере:"
echo "  sudo cp deploy/nginx/out/*.conf /etc/nginx/sites-available/"
echo "  sudo ln -s /etc/nginx/sites-available/${PUBLIC_DOMAIN}.conf /etc/nginx/sites-enabled/"
echo "  sudo ln -s /etc/nginx/sites-available/${ADMIN_DOMAIN}.conf /etc/nginx/sites-enabled/"
echo "  sudo nginx -t && sudo systemctl reload nginx"
