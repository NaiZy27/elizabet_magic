#!/usr/bin/env bash
# Собрать конфиги nginx под свои домены.
#
# Запуск из корня проекта:
#   ./deploy/render-nginx.sh elizabet.example crm.elizabet.example
#
# Сертификат выпускается один на оба имени:
#   certbot certonly --nginx -d elizabet.example -d crm.elizabet.example
# certbot кладёт его в папку по первому имени — /etc/letsencrypt/live/elizabet.example/,
# поэтому и конфиг админки ссылается на неё. Если сертификаты выпускались по отдельности,
# имя папки для админки можно передать третьим аргументом.
#
# Готовые файлы кладутся в deploy/nginx/out/ — оттуда их копируют
# в /etc/nginx/sites-available на сервере.

set -euo pipefail

PUBLIC_DOMAIN="${1:-}"
ADMIN_DOMAIN="${2:-}"
ADMIN_CERT="${3:-${PUBLIC_DOMAIN}}"

if [ -z "${PUBLIC_DOMAIN}" ] || [ -z "${ADMIN_DOMAIN}" ]; then
  echo "Укажите оба домена: ./deploy/render-nginx.sh ваш-домен crm.ваш-домен" >&2
  exit 1
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${HERE}/nginx/out"
mkdir -p "${OUT}"

sed "s/example\.ru/${PUBLIC_DOMAIN}/g" "${HERE}/nginx/pay.conf" > "${OUT}/${PUBLIC_DOMAIN}.conf"
# В конфиге админки: путь к сертификату — на общий, домен — на поддомен админки.
sed -e "s#/etc/letsencrypt/live/crm\.example\.ru/#/etc/letsencrypt/live/${ADMIN_CERT}/#g" \
    -e "s/crm\.example\.ru/${ADMIN_DOMAIN}/g" \
    "${HERE}/nginx/crm.conf" > "${OUT}/${ADMIN_DOMAIN}.conf"

# Сертификат должен существовать, иначе nginx -t упадёт — лучше сказать об этом сразу.
for cert in "${PUBLIC_DOMAIN}" "${ADMIN_CERT}"; do
  if [ -d /etc/letsencrypt ] && [ ! -e "/etc/letsencrypt/live/${cert}/fullchain.pem" ]; then
    echo "Внимание: нет сертификата /etc/letsencrypt/live/${cert}/ — сначала выпустите его certbot" >&2
  fi
done

echo "Готово:"
echo "  ${OUT}/${PUBLIC_DOMAIN}.conf  — страница оплаты и вебхуки"
echo "  ${OUT}/${ADMIN_DOMAIN}.conf   — админка"
echo
echo "Дальше на сервере:"
echo "  sudo cp deploy/nginx/out/*.conf /etc/nginx/sites-available/"
echo "  sudo ln -s /etc/nginx/sites-available/${PUBLIC_DOMAIN}.conf /etc/nginx/sites-enabled/"
echo "  sudo ln -s /etc/nginx/sites-available/${ADMIN_DOMAIN}.conf /etc/nginx/sites-enabled/"
echo "  sudo nginx -t && sudo systemctl reload nginx"
