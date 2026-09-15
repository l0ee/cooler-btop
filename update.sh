#!/usr/bin/env bash
set -e

echo "[*] Updating the Cooler btop source checkout..."

# Check if git exists
if ! command -v git &> /dev/null; then
    echo "Git is not installed. Please install git."
    exit 1
fi

echo "[*] Pulling latest changes from master..."
git pull origin master || git pull origin main

echo "[*] Building a local wheel without installing it..."
"$(dirname "$0")/install.sh"

echo "[*] Update complete. No daemon service was changed or started."
