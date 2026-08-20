#!/bin/sh
# WP-Bot custom entrypoint: Apply Apache configuration fixes before starting WordPress

echo "[entrypoint] Applying Apache configuration fixes..."

# Fix AllowOverride for .htaccess support (REST API rewrite rules)
sed -i 's/AllowOverride None/AllowOverride All/g' /etc/apache2/apache2.conf 2>/dev/null || true

# Enable mod_rewrite if not already enabled
a2enmod rewrite >/dev/null 2>&1 || true

echo "[entrypoint] Starting WordPress..."

# Call the original WordPress entrypoint
exec docker-entrypoint.sh apache2-foreground
