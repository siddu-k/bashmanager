#!/bin/bash
# name: Base64 Utility
# desc: Quick base64 encoder and decoder.
# tag: dev-tools

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${BLUE}====================================================${NC}"
echo -e "${GREEN}               BASE64 UTILITY                       ${NC}"
echo -e "${BLUE}====================================================${NC}"

echo -e "Select action:"
echo -e "1) ${YELLOW}Encode${NC} text to Base64"
echo -e "2) ${YELLOW}Decode${NC} Base64 to text"
read -p "Enter Choice (1 or 2): " CHOICE

if [ "${CHOICE}" = "1" ]; then
    read -p "Enter text to encode: " PLAIN_TEXT
    echo -n "${PLAIN_TEXT}" | base64
elif [ "${CHOICE}" = "2" ]; then
    read -p "Enter base64 string to decode: " B64_TEXT
    echo -n "${B64_TEXT}" | base64 --decode
    echo ""
else
    echo "Invalid option selected."
fi
