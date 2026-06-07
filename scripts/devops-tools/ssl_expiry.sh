#!/bin/bash
# name: SSL Expiry Checker
# desc: Check and verify SSL/TLS certificate validity and expiration for any web host.
# tag: devops-tools, ssl
# url: https://www.ssllabs.com/ssltest/

echo "=== SSL Expiry Checker ==="
read -p "Enter domain (e.g. google.com): " domain
if [ -z "$domain" ]; then
    domain="google.com"
fi
echo "Connecting to $domain:443..."
echo ""
if ! command -v openssl >/dev/null 2>&1; then
    echo "Error: openssl utility is not installed."
    exit 1
fi
res=$(echo | openssl s_client -servername "$domain" -connect "$domain":443 2>/dev/null | openssl x509 -noout -dates -issuer)
if [ -z "$res" ]; then
    echo "Failed to retrieve SSL certificate details."
else
    echo "$res"
fi
