#!/usr/bin/env bash
cd "$(dirname "$0")" || exit 1
PY="${PYTHON:-python3}"
echo "TCP Listener, Dashboard Web Server, Simulation Server, Live Monitor Server & Compute Server initializing..."
"$PY" run.py listener 8889 &
"$PY" run.py dash 9000 &
"$PY" run.py sim 9001 &
"$PY" run.py live 9002 &
"$PY" run.py compute 9003 &
wait
