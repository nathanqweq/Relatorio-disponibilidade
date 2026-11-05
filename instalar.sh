#!/bin/sh
# Instala ou atualiza os Relatórios de Disponibilidade neste servidor (Debian). Rode como root.
set -e
pasta=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
if ! command -v python3 >/dev/null 2>&1; then
    apt-get update
    apt-get install -y python3
fi
exec python3 "$pasta/instalar.py" "$@"
