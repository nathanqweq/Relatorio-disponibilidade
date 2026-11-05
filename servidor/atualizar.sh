#!/bin/bash
# Atualiza o banco: itens, quedas (desde o 1º dia do mês anterior), chamados e limpeza.
# Roda a cada 10 minutos pelo timer relatorios-engie-atualizar.
set -e
cd /etc/relatorios-engie
python3 disp.py sync-objetos
python3 disp.py importar-eventos --de "$(date -d "$(date +%Y-%m-01) -1 month" +%F)" --ate "$(date +%F)"
python3 disp.py importar-chamados
python3 disp.py limpar --meses 3
