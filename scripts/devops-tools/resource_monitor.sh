#!/bin/bash
# name: Resource Alarm Monitor
# desc: Continuously checks CPU, Memory, and Disk, throwing colorful alerts when thresholds are breached.
# tag: devops-tools

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

CPU_THRESHOLD=80
MEM_THRESHOLD=85
DISK_THRESHOLD=90

echo -e "${BLUE}====================================================${NC}"
echo -e "${GREEN}             RESOURCE ALARM MONITOR                 ${NC}"
echo -e "${BLUE}====================================================${NC}"

echo -e "CPU Alert Limit: ${YELLOW}${CPU_THRESHOLD}%${NC}"
echo -e "RAM Alert Limit: ${YELLOW}${MEM_THRESHOLD}%${NC}"
echo -e "Disk Alert Limit: ${YELLOW}${DISK_THRESHOLD}%${NC}\n"

DISK_USAGE=$(df -h / | tail -1 | awk '{print $5}' | sed 's/%//')
if [ "${DISK_USAGE}" -gt "${DISK_THRESHOLD}" ]; then
    echo -e "${RED}[ALERT] Disk usage is critically high: ${DISK_USAGE}%${NC}"
else
    echo -e "${GREEN}[OK] Disk Usage: ${DISK_USAGE}%${NC}"
fi

if [ "$(uname)" = "Darwin" ]; then
    MEM_USAGE=$(memory_pressure | grep "System-wide memory free percentage" | awk '{print 100 - $5}')
    if [ -z "${MEM_USAGE}" ]; then
        MEM_USAGE=$(vm_stat | awk '/free/ {free=$3} /active/ {active=$3} /inactive/ {inactive=$3} END {total=free+active+inactive; print (active+inactive)/total*100}' | cut -d. -f1)
    fi
else
    MEM_USAGE=$(free | grep Mem | awk '{print int($3/$2 * 100)}')
fi

if [ -n "${MEM_USAGE}" ]; then
    if [ "${MEM_USAGE}" -gt "${MEM_THRESHOLD}" ]; then
        echo -e "${RED}[ALERT] Memory usage is critically high: ${MEM_USAGE}%${NC}"
    else
        echo -e "${GREEN}[OK] Memory Usage: ${MEM_USAGE}%${NC}"
    fi
fi

if [ "$(uname)" = "Darwin" ]; then
    CPU_USAGE=$(ps -A -o %cpu | awk '{s+=$1} END {print int(s)}')
else
    CPU_USAGE=$(top -bn1 | grep "Cpu(s)" | sed "s/.*, *\([0-9.]*\)%* id.*/\1/" | awk '{print int(100 - $1)}')
fi

if [ -n "${CPU_USAGE}" ]; then
    if [ "${CPU_USAGE}" -gt "${CPU_THRESHOLD}" ]; then
        echo -e "${RED}[ALERT] CPU load is high: ${CPU_USAGE}%${NC}"
    else
        echo -e "${GREEN}[OK] CPU Load: ${CPU_USAGE}%${NC}"
    fi
fi
