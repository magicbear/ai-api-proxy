#!/bin/bash

# API Proxy Server Startup Script for Screen
# Run this script inside a screen session

cd /data/clawdata/dev/proj/api-proxy

echo "Starting API Proxy Server..."
echo "Server will be available at http://0.0.0.0:16900"
echo "Monitor interface available at http://0.0.0.0:16900/monitor"
echo "Press Ctrl+A, then D to detach from screen session"
echo ""

# Run the Python server directly
python3 proxy_server.py