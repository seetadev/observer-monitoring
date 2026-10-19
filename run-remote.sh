#!/usr/bin/env bash
cd "$(dirname "$0")" || exit 1
"${PYTHON:-python3}" run.py remote 8888
