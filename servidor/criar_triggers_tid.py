#!/usr/bin/env python3
"""
Cria as triggers das metas TID (uma por item) nos hosts "ENGIE APP - TID *", já com as tags
do banco de disponibilidade. Não duplica: pula itens que já têm a trigger.

    python3 criar_triggers_tid.py              # simulação: só mostra o que faria
    python3 criar_triggers_tid.py --aplicar    # cria

Regras
  net.tcp.service[...]  -> problema quando as 3 últimas coletas = 0     max(/host/key,#3)=0
  web.test.fail[...]    -> problema quando as 3 últimas coletas > 0     min(/host/key,#3)>0
Tags: relatorio=tid-aplicacoes, grupo=<nome do host>, item=<componente>

Usa ZABBIX_URL / ZABBIX_TOKEN / ZABBIX_VERIFY_TLS do ambiente (mesmo env do disp.py).
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from disp import Zabbix  # noqa: E402

PREFIXO_HOST = "ENGIE APP - TID"
SEVERIDADE = 4  # 4 = High; 5 = Disaster


def componente(item):
    k = item["key_"]
    if k.startswith("web.test.fail["):
        return "Cenário web: " + k[len("web.test.fail["):-1]
    return item["name"]


def nome_trigger(item):
    k = item["key_"]
    if k.startswith("web.test.fail["):
        return f'TID - cenário "{k[len("web.test.fail["):-1]}" com falha'
    return f"TID - {item['name']} indisponível"


def expressao(host, item):
    k = item["key_"]
    if k.startswith("web.test.fail["):
        return f"min(/{host}/{k},#3)>0"
    return f"max(/{host}/{k},#3)=0"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--aplicar", action="store_true", help="cria de fato (sem isso é simulação)")
    ap.add_argument("--severidade", type=int, default=SEVERIDADE, choices=range(6))
    a = ap.parse_args()

    zbx = Zabbix()
    hosts = zbx.call("host.get", {"output": ["hostid", "host", "name"],
                                  "search": {"name": PREFIXO_HOST}, "startSearch": True})
    if not hosts:
        sys.exit(f"Nenhum host começando com '{PREFIXO_HOST}'")
    por_id = {h["hostid"]: h for h in hosts}

    itens = zbx.call("item.get", {"output": ["itemid", "hostid", "name", "key_", "status"],
                                  "hostids": list(por_id), "webitems": True,
                                  "filter": {"status": 0}})
    itens = [i for i in itens if re.match(r"(net\.tcp\.service|web\.test\.fail)\[", i["key_"])]

    existentes = zbx.call("trigger.get", {"output": ["triggerid", "description"], "hostids": list(por_id),
                                          "selectFunctions": ["itemid"]})
    com_trigger = {f["itemid"] for t in existentes for f in t.get("functions", [])}

    criar, pulados = [], 0
    for i in sorted(itens, key=lambda x: (por_id[x["hostid"]]["name"], x["key_"])):
        if i["itemid"] in com_trigger:
            pulados += 1
            continue
        h = por_id[i["hostid"]]
        criar.append({
            "description": nome_trigger(i),
            "expression": expressao(h["host"], i),
            "priority": a.severidade,
            "manual_close": 0,
            "tags": [{"tag": "relatorio", "value": "tid-aplicacoes"},
                     {"tag": "grupo", "value": h["name"]},
                     {"tag": "item", "value": componente(i)}],
        })

    for t in criar:
        tags = {x["tag"]: x["value"] for x in t["tags"]}
        print(f"+ {tags['grupo']:<30} {t['description']}\n    {t['expression']}")
    print(f"\n{len(criar)} trigger(s) a criar; {pulados} item(ns) já tinham trigger.")

    if a.aplicar and criar:
        r = zbx.call("trigger.create", criar)
        print(f"Criadas: {len(r['triggerids'])}")
    elif criar:
        print("Simulação. Rode com --aplicar para criar.")


if __name__ == "__main__":
    main()
