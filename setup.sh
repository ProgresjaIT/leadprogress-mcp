#!/bin/bash

if command -v python &> /dev/null; then
    PYTHON_CMD="python"
elif command -v python3 &> /dev/null; then
    PYTHON_CMD="python3"
else
    echo "Błąd: Nie znaleziono Pythona ani Python3 w systemie." >&2
    exit 1
fi

$PYTHON_CMD -m venv venv
venv/bin/pip install -r requirements.txt
