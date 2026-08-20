#!/bin/sh
# WP-Bot sandbox setup (runs in the wordpress:cli container).
#
# Installs WordPress if needed, then creates a dedicated EDITOR-level
# user ("editor") with a generated APPLICATION PASSWORD. No admin
# credentials are used by the tests. The password is written to
# /shared/app-password.txt (mounted at wp-sandbox/_output/) and echoed.
set -e

WP="wp --allow-root --path=/var/www/html"

echo "[setup] waiting for WordPress to come up..."
i=0
until curl -sf -o /dev/null http://wordpress:80/; do
  i=$((i + 1))
  [ "$i" -gt 60 ] && echo "[setup] timed out waiting for WordPress" && exit 1
  sleep 2
done

if ! $WP core is-installed; then
  echo "[setup] installing WordPress..."
  $WP core install \
    --url="$WP_URL" \
    --title="WP-Bot Sandbox" \
    --admin_user=admin \
    --admin_password='AdminPass123!' \
    --admin_email=admin@example.com \
    --skip-email
fi

# Set permalink structure for REST API
echo "[setup] setting permalink structure..."
$WP rewrite structure '/%postname%/' 2>/dev/null || true
$WP rewrite flush 2>/dev/null || true

if ! $WP user get editor >/dev/null 2>&1; then
  echo "[setup] creating Editor user..."
  $WP user create editor editor@example.com \
    --role=editor --user_pass='EditorPass123!' --porcelain >/dev/null
fi

# Application password (wp-cli 2.7+): the ONLY credential the tests use.
APP_PASS=$($WP user application-password create editor wpbot --porcelain)
mkdir -p /shared
echo "$APP_PASS" > /shared/app-password.txt
chmod 600 /shared/app-password.txt

echo "[setup] ready. Editor application password: $APP_PASS"

# --- Fix Apache configuration for REST API and Application Passwords ---
echo "[setup] configuring Apache..."
sed -i 's/AllowOverride None/AllowOverride All/g' /etc/apache2/apache2.conf 2>/dev/null || true
service apache2 reload >/dev/null 2>&1 || true

echo "[setup] done."
