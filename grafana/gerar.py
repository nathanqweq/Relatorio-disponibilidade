import copy, json, sys
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from comum import *

# ------------------------------------------------------------------ LINKS (internet e MPLS/BGP)
REL = "relatorio=${tipo}"
GRUPO = "grupo=${operadora:percentencode}"
operadoras = """<div class="ops">
{{#each data}}
  <div class="op {{classe DISPONIBILIDADE}}">
    <div class="n" title="{{NAME}}">{{NAME}}</div>
    <div class="v">{{pct DISPONIBILIDADE}}</div>
    <div class="s">{{OBJETOS}} link(s) · {{INCIDENTES}} queda(s) · {{CHAMADOS}} chamado(s) · fora {{dur SEGUNDOS_FORA}}</div>
  </div>
{{/each}}
</div>
<div class="legenda">Média simples dos links de cada operadora · verde 100% · laranja abaixo de 100% e dentro da meta de 99,90% · vermelho abaixo da meta</div>"""
top10 = tabela("Link", fonte="(top data 10)")
el = {
  "panel-1": painel(1, "Resumo", f"{API}/disponibilidade?{REL}&{PER}&{GRUPO}", resumo_kpis("Disponibilidade geral dos links", "links", "{{operadoras}} operadoras")),
  "panel-2": painel(2, "Disponibilidade por operadora", f"{API}/disponibilidade?{REL}&{PER}&{GRUPO}&por=grupo", operadoras, "Pior operadora primeiro"),
  "panel-3": painel(3, "Links", f"{API}/disponibilidade?{REL}&{PER}&{GRUPO}", tabela("Link", "Designação / velocidade", "DESCRICAO"),
                    "Pior link primeiro. Tempo fora já descontados os expurgos aprovados."),
  "panel-4": painel(4, "Quedas no período", f"{API}/incidentes?{REL}&{PER}&{GRUPO}&ordem=desc", QUEDAS, "Mais recentes primeiro"),
  "panel-5": painel(5, "Top 10 piores links", f"{API}/disponibilidade?{REL}&{PER}&{GRUPO}", top10, "Os 10 links com menor disponibilidade no período"),
}
layout = [item("panel-1", 0, 0, 24, 5), item("panel-2", 0, 5, 24, 11),
          item("panel-3", 0, 16, 14, 24), item("panel-5", 14, 16, 10, 11), item("panel-4", 14, 27, 10, 13)]
tipo = {"kind": "CustomVariable", "spec": {
    "name": "tipo", "label": "Tipo", "hide": "dontHide", "skipUrlSync": False,
    "query": "Internet : links-internet, MPLS / BGP : links-mpls",
    "current": {"text": "Internet", "value": "links-internet"},
    "options": [{"text": "Internet", "value": "links-internet", "selected": True},
                {"text": "MPLS / BGP", "value": "links-mpls", "selected": False}],
    "multi": False, "includeAll": False, "allowCustomValue": False}}
operadora = {"kind": "QueryVariable", "spec": {
    "name": "operadora", "label": "Operadora", "hide": "dontHide", "skipUrlSync": False,
    "current": {"text": "All", "value": "$__all"}, "options": [],
    "includeAll": True, "allValue": "*", "multi": False, "allowCustomValue": False,
    "refresh": "onTimeRangeChanged", "regex": "", "sort": "alphabeticalAsc", "definition": "",
    "query": {"kind": "DataQuery", "group": "yesoreyeram-infinity-datasource", "version": "v0",
              "datasource": {"name": "dg2gfvwh9s0sgb"},
              "spec": {"queryType": "infinity", "refId": "variable",
                       "infinityQuery": {"type": "json", "source": "url", "format": "table", "parser": "backend",
                                         "url": f"{API}/disponibilidade?{REL}&{PER}&por=grupo",
                                         "url_options": {"method": "GET", "data": "", "params": [], "headers": []},
                                         "root_selector": "", "filters": [],
                                         "columns": [{"selector": "NAME", "text": "NAME", "type": "string"}]}}}}}
d = dashboard("engie-disp-links", "ENGIE - Disponibilidade Links",
              "Links de internet e MPLS/BGP por operadora (relatórios Links Internet, Links BGP e Operadoras)",
              ["ENGIE", "disponibilidade", "links", "sqlite"], el, layout, [tipo, operadora])
json.dump(d, open("ENGIE_Disponibilidade_Links_sqlite.json", "w"), ensure_ascii=False, indent=2)

# ------------------------------------------------------------------ FIREWALLS SCI
el = {
  "panel-1": painel(1, "Resumo", f"{API}/disponibilidade?relatorio=firewalls-sci&{PER}", resumo_kpis("Disponibilidade geral dos firewalls", "firewalls", "VIPs SCI")),
  "panel-2": painel(2, "Firewalls SCI", f"{API}/disponibilidade?relatorio=firewalls-sci&{PER}", tabela("Firewall"),
                    "Pior firewall primeiro. Tempo fora já descontados os expurgos aprovados."),
  "panel-3": painel(3, "Quedas no período", f"{API}/incidentes?relatorio=firewalls-sci&{PER}&ordem=desc", QUEDAS, "Mais recentes primeiro"),
}
layout = [item("panel-1", 0, 0, 24, 5), item("panel-2", 0, 5, 13, 30), item("panel-3", 13, 5, 11, 30)]
d = dashboard("engie-disp-firewalls", "ENGIE - Disponibilidade Firewalls SCI",
              "Firewalls SCI (VIP) da rede industrial (relatórios Firewalls SCII e Mensal Firewalls SCI)",
              ["ENGIE", "disponibilidade", "firewalls", "sqlite"], el, layout, [])
json.dump(d, open("ENGIE_Disponibilidade_Firewalls_SCI_sqlite.json", "w"), ensure_ascii=False, indent=2)

# ------------------------------------------------------------------ TID (+ Switches Core GA e NE)
tid = copy.deepcopy(base)
e = tid["spec"]["elements"]
q = lambda p: e[p]["spec"]["data"]["spec"]["queries"][0]["spec"]["query"]["spec"]
q("panel-2")["url"] = f"{API}/resumo?{PER}&relatorios=tid-usinas,tid-aplicacoes&unidade_grupo=tid-aplicacoes"
novos = {
  "panel-3": painel(3, "Disponibilidade Usinas", f"{API}/disponibilidade?relatorio=tid-usinas&grupo=GERACAO&{PER}&formato=powerplant&cliente=${{cliente}}", tabela("Usina")),
  "panel-4": painel(4, "Disponibilidade Usinas Transmissão", f"{API}/disponibilidade?relatorio=tid-usinas&grupo=TRANSMISSAO&{PER}&formato=powerplant&cliente=${{cliente}}", tabela("Usina")),
  "panel-5": painel(5, "Disponibilidade aplicações", f"{API}/disponibilidade?relatorio=tid-aplicacoes&por=grupo&{PER}", tabela("Aplicação")),
}
gane = ('<div class="duas-col"><div>'
        '<div class="reg {{classe (mediaDisp (prefixo data "GA"))}}">Gralha Azul<span class="v">{{pct (mediaDisp (prefixo data "GA"))}}</span></div>'
        + tabela("Usina", fonte='(prefixo data "GA")') + '</div><div>'
        '<div class="reg {{classe (mediaDisp (prefixo data "NE"))}}">Novo Estado<span class="v">{{pct (mediaDisp (prefixo data "NE"))}}</span></div>'
        + tabela("Usina", fonte='(prefixo data "NE")') + '</div></div>')
novos["panel-6"] = painel(6, "Switches Core GA e NE", f"{API}/disponibilidade?relatorio=tid-usinas&prefixo=GA,NE&{PER}&formato=powerplant&cliente=${{cliente}}", gane,
                          "Recorte de Gralha Azul e Novo Estado (relatórios Switches Core GA e NE)")
novos["panel-7"] = painel(7, "Quedas no período — usinas", f"{API}/incidentes?relatorio=tid-usinas&{PER}&ordem=desc", QUEDAS, "Mais recentes primeiro")
e.update(novos)
tid["spec"]["layout"]["spec"]["items"] = [
    item("panel-2", 0, 0, 24, 5), item("panel-3", 0, 5, 12, 30), item("panel-4", 12, 5, 12, 20),
    item("panel-5", 12, 25, 12, 10), item("panel-6", 0, 35, 24, 12), item("panel-7", 0, 47, 24, 12)]
tid["spec"]["timeSettings"].update({"from": "now/M", "to": "now/M"})
tid = json.loads(json.dumps(tid).replace("__INFINITY_UID__", "dg2gfvwh9s0sgb"))
json.dump(tid, open("ENGIE_Disponibilidade_TID_sqlite.json", "w"), ensure_ascii=False, indent=2)
print("ok")
