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

# Check if openssl is installed
if ! command -v openssl >/dev/null 2>&1; then
    echo "Error: openssl utility is not installed."
    exit 1
fi

# Fetch certificate details
cert_info=$(echo | openssl s_client -servername "$domain" -connect "$domain:443" 2>/dev/null | openssl x509 -noout -dates -issuer)

# Verify certificate was retrieved
if [ -z "$cert_info" ]; then
    echo "Failed to retrieve SSL certificate details."
    exit 1
fi

# Display certificate information
echo "$cert_info"
echo ""

# Extract expiry date
expiry_date=$(echo "$cert_info" | grep "notAfter" | cut -d= -f2)

# Convert expiry date to Unix timestamp
expiry_ts=$(date -d "$expiry_date" +%s 2>/dev/null)
current_ts=$(date +%s)

# Validate timestamp conversion
if [ -z "$expiry_ts" ]; then
    echo "Unable to calculate certificate expiry."
    exit 1
fi

# Calculate remaining days
days_left=$(( (expiry_ts - current_ts) / 86400 ))

echo "Days Remaining: $days_left"
echo ""

# Display certificate health status
if [ "$days_left" -lt 0 ]; then
    echo "❌ EXPIRED: Certificate has already expired!"
elif [ "$days_left" -le 7 ]; then
    echo "🚨 CRITICAL: Certificate expires within 7 days!"
elif [ "$days_left" -le 30 ]; then
    echo "⚠ WARNING: Certificate expires within 30 days!"
else
    echo "✅ HEALTHY: Certificate is valid."
fi