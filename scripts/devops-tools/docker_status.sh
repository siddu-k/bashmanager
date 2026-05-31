#!/bin/bash
# name: Docker Status
# desc: Checks docker service status and lists containers and images.
# tag: devops-tools

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${BLUE}====================================================${NC}"
echo -e "${GREEN}             DOCKER STATUS REPORT                   ${NC}"
echo -e "${BLUE}====================================================${NC}"

if ! command -v docker &> /dev/null; then
    echo -e "${RED}Error: Docker CLI is not installed on this machine.${NC}"
    exit 1
fi

echo -e "\n${YELLOW}[Docker Daemon Health]${NC}"
if docker info &> /dev/null; then
    echo -e "${GREEN}Docker daemon is running perfectly.${NC}"
else
    echo -e "${RED}Docker daemon is not running or accessible without root.${NC}"
    exit 1
fi

echo -e "\n${YELLOW}[Running Containers]${NC}"
docker ps --format "table {{.ID}}\t{{.Names}}\t{{.Status}}\t{{.Ports}}"

echo -e "\n${YELLOW}[Resource Usage Summary]${NC}"
docker stats --no-stream --format "table {{.Container}}\t{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}" 2>/dev/null || echo "Stats unavailable"

echo -e "\n${YELLOW}[Active Images]${NC}"
docker images --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}" | head -n 10
