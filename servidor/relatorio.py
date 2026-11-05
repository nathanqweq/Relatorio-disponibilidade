#!/usr/bin/env python3
"""
Gera os relatórios em PDF a partir do banco de disponibilidade (mesmo cálculo do disp.py / api.py).
Os relatórios são definidos em relatorios.json (seções em ordem: indicadores, tabela...).

    python3 relatorio.py gerar  --relatorio tid --mes 2026-10 [--saida arquivo.pdf]
    python3 relatorio.py gerar  --todos --de 2026-10-26 --ate 2026-10-30 --saida pasta/   (período livre)
    python3 relatorio.py fechar --relatorio tid --mes 2026-10 [--por nome] [--nova-versao]
    python3 relatorio.py fechar-mes-anterior            (para o timer do dia 1º: fecha todos os relatórios)
    python3 relatorio.py parciais                       (para o timer diário: parcial do mês de cada relatório)

Pastas: PDF_DIR/AAAA/MM/            PDFs oficiais do mês (fechamento)
        PDF_DIR/AAAA/MM/parciais/   parcial do dia (um por relatório, substituído todo dia;
                                    apagado quando o relatório do mês é fechado)

Mês corrente = relatório PARCIAL (marcado com a data/hora do corte).
Mês fechado  = o PDF guardado no fechamento é o oficial; gerar de novo exige --nova-versao.

Variáveis: DISP_DB, PDF_DIR (padrão: ./pdf ao lado deste arquivo)
Dependência: apt install weasyprint
"""
import argparse
import html
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import api   # noqa: E402
import disp  # noqa: E402

BASE = Path(__file__).resolve().parent
DEFS = BASE / "relatorios.json"
PDF_DIR = Path(os.getenv("PDF_DIR", BASE / "pdf"))
MESES = ["Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho", "Julho",
         "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"]


def definicoes():
    return json.loads(DEFS.read_text(encoding="utf-8"))


def e(t):
    return html.escape("" if t is None else str(t))


def fmt_pct(v):
    if v is None:
        return "—"
    return "100%" if v >= 100 else f"{v:.2f}".replace(".", ",") + "%"


def classe(v, meta):
    if v is None:
        return "sem"
    if v >= 100:
        return "ok"
    return "atencao" if v >= meta else "critico"


# ------------------------------------------------------------ dados ----

def montar(conn, defn, mes):
    """Executa as seções e devolve [(secao, dados)] + valores por objeto (para o fechamento)."""
    saida, relatorios = [], set()
    for s in defn["secoes"]:
        if s["tipo"] == "indicadores":
            dados = {}
            if s.get("relatorios"):
                q = {"mes": mes, "relatorios": ",".join(s["relatorios"]),
                     "unidade_grupo": ",".join(s.get("unidade_grupo", []))}
                dados = api.resumo(conn, q)[0]
                relatorios.update(s["relatorios"])
            for i in s["indicadores"]:
                if i.get("relatorio"):   # indicador próprio: média dos itens filtrados por grupo/prefixo
                    linhas = api.filtrar_prefixo(disp.calcular(conn, i["relatorio"], mes, grupo=i.get("grupo")),
                                                 i.get("prefixo"))
                    dados[i["chave"]] = api.pct(api.media([l["disponibilidade"] for l in linhas]))
                    relatorios.add(i["relatorio"])
            saida.append((s, dados))
        elif s["tipo"] == "tabela":
            q = {"mes": mes, "relatorio": s["relatorio"], "cliente": defn.get("cliente", "")}
            for k in ("grupo", "prefixo", "por", "formato"):
                if s.get(k):
                    q[k] = s[k]
            dados = api.disponibilidade(conn, q)
            if s.get("limite"):         # ex.: Top 10 piores
                dados = dados[:int(s["limite"])]
            saida.append((s, dados))
            relatorios.add(s["relatorio"])
        elif s["tipo"] == "grupos":
            # um bloco por grupo (operadora): valor do grupo + tabela dos itens
            base = {"mes": mes, "relatorio": s["relatorio"], "cliente": defn.get("cliente", "")}
            if s.get("prefixo"):
                base["prefixo"] = s["prefixo"]
            resumo = api.disponibilidade(conn, dict(base, por="grupo"))
            itens = api.disponibilidade(conn, dict(base, formato=s.get("formato", "")))
            if s.get("ordem", "nome") == "nome":
                resumo.sort(key=lambda g: (g["NAME"] or "").upper())
            blocos = [(g, [l for l in itens if (l["GRUPO"] or "(sem grupo)") == g["NAME"]]) for g in resumo]
            if s.get("grupos"):          # só alguns grupos, na ordem pedida
                por_nome = {g["NAME"]: (g, ls) for g, ls in blocos}
                blocos = [por_nome[n] for n in s["grupos"] if n in por_nome]
            if s.get("evidencias"):      # evidências de cada grupo logo abaixo da tabela dele
                blocos = [(g, ls, evidencias(conn, defn, dict(base, grupo=g["NAME"], formato=s.get("formato", "")), mes))
                          for g, ls in blocos]
            else:
                blocos = [(g, ls, None) for g, ls in blocos]
            saida.append((s, blocos))
            relatorios.add(s["relatorio"])
        elif s["tipo"] == "evidencias":
            saida.append((s, evidencias(conn, defn, s, mes)))
            relatorios.add(s["relatorio"])
        elif s["tipo"] == "chamados":
            saida.append((s, chamados_mes(conn, defn, s, mes)))
            relatorios.add(s["relatorio"])
        else:
            raise SystemExit(f"tipo de seção desconhecido: {s['tipo']}")
    valores = []
    for rel in sorted(relatorios):
        valores += disp.calcular(conn, rel, mes)
    return saida, valores


def evidencias(conn, defn, s, mes):
    """Itens abaixo de 100% no mês, cada um com as quedas, o chamado e a ação registrada pelo analista."""
    q = {"mes": mes, "relatorio": s["relatorio"], "cliente": defn.get("cliente", "")}
    for k in ("grupo", "prefixo", "formato"):
        if s.get(k):
            q[k] = s[k]
    m_ini, m_fim = disp.limites_mes(mes)
    agora = int(time.time())
    fmt = lambda t: datetime.fromtimestamp(t, disp.TZ).strftime("%d/%m/%Y %H:%M:%S") if t else "em aberto"
    out = []
    for l in api.disponibilidade(conn, q):
        if l["DISPONIBILIDADE"] is None:
            continue
        quedas = []
        for r in conn.execute(
                "SELECT i.id, i.inicio, i.fim, i.problema, i.chamado, c.titulo, c.numero AS importado "
                "FROM incidentes i JOIN objetos o ON o.id = i.objeto_id "
                "LEFT JOIN chamados c ON c.numero = i.chamado "
                "WHERE o.relatorio = ? AND o.item = ? AND i.inicio < ? AND coalesce(i.fim, ?) > ? ORDER BY i.inicio",
                (s["relatorio"], l["ITEM"], m_fim, agora, m_ini)):
            cob, _ = disp.situacao_expurgo(conn, r["id"])
            if not r["chamado"]:
                acao = "Sem chamado registrado."
            elif not r["importado"]:
                acao = "Chamado ainda não consultado no TOPdesk."
            else:
                acao = disp.acao_principal(conn, r["chamado"]) or "Sem ação registrada no chamado."
            quedas.append({"chamado": r["chamado"] or "—", "descricao": r["titulo"] or r["problema"] or "",
                           "down": fmt(r["inicio"]), "up": fmt(r["fim"]),
                           "minutos": int(((r["fim"] or agora) - r["inicio"]) // 60), "acao": acao,
                           "expurgo": cob, "motivo_expurgo": disp.descricao_expurgo(conn, r["id"]) if cob else None})
        # entra na evidência quem ficou abaixo de 100% ou teve queda desconsiderada (para mostrar o motivo)
        if l["DISPONIBILIDADE"] < 100 or any(x["expurgo"] for x in quedas):
            out.append((l, quedas))
    return out


def chamados_mes(conn, defn, s, mes):
    """Todas as quedas do mês que geraram chamado (acionamentos), mais recentes primeiro."""
    m_ini, m_fim = disp.limites_mes(mes)
    agora = int(time.time())
    fmt = lambda t: datetime.fromtimestamp(t, disp.TZ).strftime("%d/%m/%Y %H:%M:%S") if t else "em aberto"
    sql = ("SELECT i.id, o.nome, o.grupo, i.inicio, i.fim, i.problema, i.chamado, c.titulo, c.numero AS importado "
           "FROM incidentes i JOIN objetos o ON o.id = i.objeto_id LEFT JOIN chamados c ON c.numero = i.chamado "
           "WHERE o.relatorio = ? AND i.chamado IS NOT NULL AND i.inicio < ? AND coalesce(i.fim, ?) > ?")
    args = [s["relatorio"], m_fim, agora, m_ini]
    if s.get("grupo"):
        sql += " AND o.grupo = ?"
        args.append(s["grupo"])
    out = []
    for r in conn.execute(sql + " ORDER BY i.inicio DESC", args):
        cob, _ = disp.situacao_expurgo(conn, r["id"])
        acao = (disp.acao_principal(conn, r["chamado"]) if r["importado"] else None) or \
            ("Chamado ainda não consultado no TOPdesk." if not r["importado"] else "Sem ação registrada no chamado.")
        out.append({"chamado": r["chamado"], "item": api.nome_exibicao(r["nome"], s.get("formato"), defn.get("cliente", "")),
                    "grupo": r["grupo"], "descricao": r["titulo"] or r["problema"] or "",
                    "down": fmt(r["inicio"]), "up": fmt(r["fim"]),
                    "minutos": int(((r["fim"] or agora) - r["inicio"]) // 60), "acao": acao,
                    "expurgo": cob, "motivo_expurgo": disp.descricao_expurgo(conn, r["id"]) if cob else None})
    return out


def marca_expurgo(q):
    """Linha marcada e texto de aviso para queda desconsiderada (inteira ou em parte)."""
    if not q.get("expurgo"):
        return "", ""
    rotulo = "NÃO CONTABILIZADO" if q["expurgo"] == "total" else "PARCIALMENTE NÃO CONTABILIZADO"
    return ' class="exp"', f'<span class="selo">{rotulo}</span> {e(q["motivo_expurgo"])}<br>'


# ------------------------------------------------------------- HTML ----

CSS = """
@page { size: A4; margin: 16mm 14mm 18mm 14mm;
        @bottom-left { content: "__RODAPE__"; font-size: 7.5pt; color: #6b7684; }
        @bottom-right { content: "Página " counter(page) " de " counter(pages); font-size: 7.5pt; color: #6b7684; } }
body { font-family: "DejaVu Sans", Arial, sans-serif; font-size: 9.5pt; color: #1d2733; }
.secao.junta { break-inside: avoid; }
.capa { border-top: 5px solid #00AAFF; padding-top: 12px; margin-bottom: 14px; border-bottom: 1px solid #dfe4ea; padding-bottom: 10px; }
.cliente { font-size: 8.5pt; letter-spacing: .12em; text-transform: uppercase; color: #00AAFF; font-weight: bold; }
h1 { font-size: 19pt; color: #0B2E4F; margin: 3px 0 2px; }
.sub { color: #6b7684; font-size: 10pt; }
.periodo { margin-top: 8px; font-size: 9.5pt; }
.parcial { display: inline-block; margin-left: 8px; padding: 1px 7px; border-radius: 3px; background: #fdf1d8; color: #8a5a00; font-size: 8pt; font-weight: bold; }
h2 { font-size: 11.5pt; color: #0B2E4F; margin: 16px 0 6px; padding-bottom: 3px; border-bottom: 2px solid #00AAFF; break-after: avoid; }
.kpis { width: 100%; border-collapse: separate; border-spacing: 8px 0; margin: 0 -8px; }
.kpi { background: #f4f6f9; border-top: 3px solid #6b7684; text-align: center; padding: 10px 6px; width: 25%; vertical-align: top; }
.kpi .v { font-size: 17pt; font-weight: bold; }
.kpi .r { font-size: 7.5pt; color: #6b7684; margin-top: 3px; }
.kpi.ok { border-top-color: #1e9e5a; } .kpi.ok .v { color: #1e9e5a; }
.kpi.atencao { border-top-color: #c98100; } .kpi.atencao .v { color: #c98100; }
.kpi.critico { border-top-color: #c62828; } .kpi.critico .v { color: #c62828; }
.kpi.sem .v { color: #9aa3ad; }
table.t { width: 100%; border-collapse: collapse; font-size: 8.6pt; }
table.t th { background: #0B2E4F; color: #fff; text-align: left; font-size: 7.5pt; text-transform: uppercase; letter-spacing: .04em; padding: 4px 6px; }
table.t td { padding: 3px 6px; border-bottom: 1px solid #dfe4ea; }
table.t tr:nth-child(even) td { background: #f4f6f9; }
table.t .n { text-align: right; white-space: nowrap; font-weight: bold; }
tr.ok .n { color: #1e9e5a; } tr.atencao .n { color: #c98100; } tr.critico .n, tr.critico td { color: #c62828; }
table.duas { width: 100%; border-collapse: collapse; } table.duas > tbody > tr > td { width: 50%; vertical-align: top; padding: 0; }
table.duas > tbody > tr > td:first-child { padding-right: 6px; } table.duas > tbody > tr > td:last-child { padding-left: 6px; }
.lead { color: #6b7684; font-size: 8.5pt; margin: 0 0 6px; }
.legenda { color: #6b7684; font-size: 7.5pt; margin-top: 4px; }
.vazio { color: #6b7684; font-style: italic; }
tr { break-inside: avoid; }
.bloco { margin: 0 0 12px; } .bloco.junta { break-inside: avoid; }
.grupo { display: flex; align-items: baseline; gap: 10px; padding: 5px 0 4px; border-bottom: 1px solid #dfe4ea; margin-bottom: 4px; break-after: avoid; }
.grupo .gn { font-weight: bold; color: #0B2E4F; font-size: 10.5pt; }
.grupo .gv { font-weight: bold; font-size: 10.5pt; } .grupo .gs { color: #6b7684; font-size: 8pt; margin-left: auto; }
.gv.ok { color: #1e9e5a; } .gv.atencao { color: #c98100; } .gv.critico { color: #c62828; } .gv.sem { color: #9aa3ad; }
table.t td.d { color: #6b7684; font-size: 7.8pt; }
.ev { margin: 4px 0 10px 18px; padding: 0; } .evid { margin: 0 0 14px; }
.ev-item { font-size: 10pt; margin: 8px 0 2px; break-after: avoid; } .ev-item.atencao b { color: #c98100; }
.ev-item.critico b { color: #c62828; } .nota { margin: 2px 0 6px; font-size: 8.5pt; color: #3d4955; break-after: avoid; }
table.ev-t { font-size: 7.6pt; table-layout: fixed; }
table.ev-t th:nth-child(1) { width: 11%; } table.ev-t th:nth-child(2) { width: 17%; } table.ev-t th:nth-child(3), table.ev-t th:nth-child(4) { width: 12.5%; }
table.ev-t th:nth-child(5) { width: 8%; } table.ev-t th:nth-child(6) { width: 39%; } table.ev-t td { vertical-align: top; } table.ev-t .nw { white-space: nowrap; }
table.ev-t td.acao { white-space: pre-line; }
table.ev-t tr.exp td { color: #8a94a0; } table.ev-t tr.exp td.n { text-decoration: line-through; }
.selo { display: inline-block; background: #eef0f3; color: #3d4955; border: 1px solid #c9d0d8; border-radius: 3px;
        padding: 0 4px; font-size: 6.8pt; font-weight: bold; letter-spacing: .03em; }
table.ch-t th:nth-child(1) { width: 10%; } table.ch-t th:nth-child(2) { width: 14%; } table.ch-t th:nth-child(3) { width: 16%; }
table.ch-t th:nth-child(4), table.ch-t th:nth-child(5) { width: 11%; } table.ch-t th:nth-child(6) { width: 7%; } table.ch-t th:nth-child(7) { width: 31%; }
.h3 { font-size: 10pt; color: #0B2E4F; margin: 10px 0 2px; break-after: avoid; } table.ev-t tr.ok td, table.ev-t td { color: #1d2733; }
"""


def tabela_html(linhas, meta, descricao=False, rotulo="Nome"):
    corpo = "".join(f'<tr class="{classe(l["DISPONIBILIDADE"], meta)}"><td>{e(l["NAME"])}</td>'
                    + (f'<td class="d">{e(l.get("DESCRICAO") or "")}</td>' if descricao else "")
                    + f'<td class="n">{fmt_pct(l["DISPONIBILIDADE"])}</td></tr>' for l in linhas)
    cab = f'<th>{e(rotulo)}</th>' + ('<th>Descrição</th>' if descricao else "") + '<th class="n">Disponibilidade</th>'
    return f'<table class="t"><thead><tr>{cab}</tr></thead><tbody>{corpo}</tbody></table>'


def evidencias_html(dados, meta):
    """Itens abaixo de 100% com nota e tabela das quedas (ou 'Sem indisponibilidade')."""
    if not dados:
        return '<ul class="ev"><li>Sem indisponibilidade;</li></ul>'
    partes = []
    for l, quedas in dados:
        com_chamado = len({q["chamado"] for q in quedas if q["chamado"] != "—"})
        nota = (f'Para o {e(l["NAME"])}, tivemos {com_chamado} chamado(s) registrado(s). '
                if com_chamado else f'Para o {e(l["NAME"])}, não tivemos chamados registrados. ')
        nota += "Seguem os eventos registrados pelo Zabbix." if quedas else ""
        n_exp = sum(1 for q in quedas if q["expurgo"])
        if n_exp:
            nota += (f" {n_exp} evento(s) desconsiderado(s) no cálculo da disponibilidade, "
                     "conforme indicado na tabela.")
        linhas = "".join(
            f'<tr{marca_expurgo(q)[0]}><td class="nw">{e(q["chamado"])}</td><td>{e(q["descricao"])}</td><td>{e(q["down"])}</td>'
            f'<td>{e(q["up"])}</td><td class="n">{q["minutos"]}</td><td class="acao">{marca_expurgo(q)[1]}{e(q["acao"])}</td></tr>'
            for q in quedas)
        tabela = (f'<table class="t ev-t"><thead><tr><th>Chamado</th><th>Descrição</th><th>Down</th><th>Up</th>'
                  f'<th class="n">Min.</th><th>Ação tomada</th></tr></thead><tbody>{linhas}</tbody></table>'
                  if quedas else "")
        partes.append(f'<div class="evid"><div class="ev-item {classe(l["DISPONIBILIDADE"], meta)}">'
                      f'&bull; {e(l["NAME"])} – <b>{fmt_pct(l["DISPONIBILIDADE"])}</b></div>'
                      f'<p class="nota"><b>Nota:</b> {nota}</p>{tabela}</div>')
    return "".join(partes)


def secao_html(s, dados, meta):
    if s["tipo"] == "indicadores":
        largura = f'{100 / len(s["indicadores"]):.0f}%'
        cards = "".join(f'<td class="kpi {classe(dados.get(i["chave"]), meta)}" style="width:{largura}">'
                        f'<div class="v">{fmt_pct(dados.get(i["chave"]))}</div>'
                        f'<div class="r">{e(i["rotulo"])}</div></td>' for i in s["indicadores"])
        return (f'<div class="secao junta"><h2>{e(s["titulo"])}</h2><table class="kpis"><tr>{cards}</tr></table>'
                f'<div class="legenda">Verde: 100% · Amarelo: abaixo de 100% e dentro da meta de {fmt_pct(meta)} · '
                f'Vermelho: abaixo da meta</div></div>')
    if s["tipo"] == "evidencias":
        return f'<div class="secao"><h2>{e(s["titulo"])}</h2>{evidencias_html(dados, meta)}</div>'
    if s["tipo"] == "chamados":
        if not dados:
            corpo = '<p class="vazio">Nenhum chamado registrado no período.</p>'
        else:
            linhas = "".join(
                f'<tr{marca_expurgo(q)[0]}><td class="nw">{e(q["chamado"])}</td><td>{e(q["item"])}</td><td>{e(q["descricao"])}</td>'
                f'<td>{e(q["down"])}</td><td>{e(q["up"])}</td><td class="n">{q["minutos"]}</td>'
                f'<td class="acao">{marca_expurgo(q)[1]}{e(q["acao"])}</td></tr>' for q in dados)
            corpo = (f'<p class="lead">{len(dados)} acionamento(s) no mês</p><table class="t ev-t ch-t"><thead><tr>'
                     f'<th>Chamado</th><th>Item</th><th>Descrição</th><th>Down</th><th>Up</th><th class="n">Min.</th>'
                     f'<th>Causa / ação tomada</th></tr></thead><tbody>{linhas}</tbody></table>')
        return f'<div class="secao"><h2>{e(s["titulo"])}</h2>{corpo}</div>'
    if s["tipo"] == "grupos":
        rotulos = s.get("rotulos", {})
        desc = bool(s.get("descricao"))
        partes = [f'<h2>{e(s["titulo"])}</h2>']
        if not dados:
            partes.append('<p class="vazio">Sem itens medidos no período.</p>')
        for g, linhas, ev in dados:
            v = g["DISPONIBILIDADE"]
            n = len(linhas)
            cab = (f'<div class="grupo"><span class="gn">{e(rotulos.get(g["NAME"], g["NAME"]))}</span>'
                   f'<span class="gv {classe(v, meta)}">{fmt_pct(v)}</span>'
                   f'<span class="gs">{n} {"item" if n == 1 else "itens"} · {g["INCIDENTES"]} queda(s)</span></div>')
            if n > 12 and not desc:
                meio = (n + 1) // 2
                corpo = (f'<table class="duas"><tr><td>{tabela_html(linhas[:meio], meta, rotulo=s.get("rotulo", "Nome"))}</td>'
                         f'<td>{tabela_html(linhas[meio:], meta, rotulo=s.get("rotulo", "Nome"))}</td></tr></table>')
            else:
                corpo = tabela_html(linhas, meta, desc, s.get("rotulo", "Nome"))
            junta = " junta" if n <= 40 else ""
            partes.append(f'<div class="bloco{junta}">{cab}{corpo}</div>')
            if ev is not None:
                titulo_ev = s.get("titulo_evidencias", "Evidências disponibilidade") + f' — {rotulos.get(g["NAME"], g["NAME"])}'
                partes.append(f'<h3 class="h3">{e(titulo_ev)}</h3>{evidencias_html(ev, meta)}')
        return '<div class="secao">' + "".join(partes) + "</div>"
    if not dados:
        return f'<div class="secao junta"><h2>{e(s["titulo"])}</h2><p class="vazio">Sem itens medidos no período.</p></div>'
    abaixo = sum(1 for l in dados if l["DISPONIBILIDADE"] is not None and l["DISPONIBILIDADE"] < meta)
    lead = f'<p class="lead">{len(dados)} itens · {abaixo} abaixo da meta de {fmt_pct(meta)}</p>'
    if len(dados) > 12:
        meio = (len(dados) + 1) // 2
        corpo = (f'<table class="duas"><tr><td>{tabela_html(dados[:meio], meta)}</td>'
                 f'<td>{tabela_html(dados[meio:], meta)}</td></tr></table>')
    else:
        corpo = tabela_html(dados, meta)
    junta = " junta" if len(dados) <= 50 else ""   # seção curta não quebra entre páginas
    return f'<div class="secao{junta}"><h2>{e(s["titulo"])}</h2>{lead}{corpo}</div>'


def rotulo_periodo(mes):
    p = disp.periodo_livre(mes)
    if p:
        d = lambda x: f"{x[8:10]}/{x[5:7]}/{x[:4]}"
        return f"{d(p[0])} 00:00 a {d(p[1])} 23:59"
    return f"{MESES[int(mes[5:7]) - 1]} de {mes[:4]}"


def documento(defn, secoes, mes, parcial_ate):
    periodo = rotulo_periodo(mes)
    meta = float(defn.get("meta", 99.9))
    marca = (f'<span class="parcial">PARCIAL · dados até {parcial_ate.strftime("%d/%m/%Y %H:%M")}</span>'
             if parcial_ate else "")
    rodape = f'{defn.get("cliente", "")} · {defn["titulo"]} · {periodo}' + (" (parcial)" if parcial_ate else "")
    corpo = "".join(secao_html(s, d, meta) for s, d in secoes)
    css = CSS.replace("__RODAPE__", rodape.replace("\\", "").replace('"', "'"))
    return (f'<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><title>{e(defn["titulo"])}</title>'
            f'<style>{css}</style></head><body>'
            f'<div class="capa"><div class="cliente">{e(defn.get("cliente", ""))}</div><h1>{e(defn["titulo"])}</h1>'
            f'<div class="sub">{e(defn.get("subtitulo", ""))}</div>'
            f'<div class="periodo">Período de referência: <strong>{periodo}</strong>{marca}</div></div>'
            f'{corpo}</body></html>')


def pasta_mes(mes, parciais=False):
    ano, m = mes.split("-")
    p = PDF_DIR / ano / m
    return p / "parciais" if parciais else p


def nome_arquivo(defn, mes, versao=None, parcial=False):
    ano, m = mes[:4], mes[5:7]
    base = defn.get("arquivo", "{relatorio} {ano}-{mes}").format(mes_nome=MESES[int(m) - 1], ano=ano, mes=m)
    p = disp.periodo_livre(mes)
    if p:                               # período livre: o nome diz os dias
        base += f" - {p[0][8:10]}-{p[0][5:7]} a {p[1][8:10]}-{p[1][5:7]}-{p[1][:4]}"
    if versao and versao > 1:
        base += f" v{versao}"
    if parcial:
        base += " (parcial)"
    return base + ".pdf"


# ------------------------------------------------------------- ações ----

def gerar(conn, rid, mes, agora=None):
    """PDF do momento. Devolve (bytes, nome_arquivo, parcial)."""
    from weasyprint import HTML
    defs = definicoes()
    if rid not in defs:
        raise KeyError(f"relatório '{rid}' não definido em relatorios.json")
    defn = defs[rid]
    agora = agora or time.time()
    parcial = agora < disp.limites_mes(mes)[1]
    secoes, _ = montar(conn, defn, mes)
    doc = documento(defn, secoes, mes, datetime.fromtimestamp(agora, disp.TZ) if parcial else None)
    return HTML(string=doc).write_pdf(), nome_arquivo(defn, mes, parcial=parcial), parcial


def pdf_oficial(conn, rid, mes):
    r = conn.execute("SELECT arquivo_pdf FROM fechamentos WHERE relatorio=? AND mes=? ORDER BY versao DESC LIMIT 1",
                     (rid, mes)).fetchone()
    return Path(r["arquivo_pdf"]) if r and r["arquivo_pdf"] and Path(r["arquivo_pdf"]).exists() else None


def fechar(conn, rid, mes, por, nova_versao=False):
    if time.time() < disp.limites_mes(mes)[1]:
        raise SystemExit(f"{mes} ainda não terminou: não dá para fechar")
    atual = conn.execute("SELECT max(versao) v FROM fechamentos WHERE relatorio=? AND mes=?", (rid, mes)).fetchone()["v"]
    if atual and not nova_versao:
        print(f"{rid} {mes} já fechado (v{atual}); use --nova-versao para revisar")
        return None
    versao = (atual or 0) + 1
    defn = definicoes()[rid]
    conteudo, _, _ = gerar(conn, rid, mes)
    _, valores = montar(conn, defn, mes)
    pasta = pasta_mes(mes)
    pasta.mkdir(parents=True, exist_ok=True)
    caminho = pasta / nome_arquivo(defn, mes, versao=versao)
    caminho.write_bytes(conteudo)
    fid = conn.execute("INSERT INTO fechamentos (relatorio, mes, versao, gerado_por, arquivo_pdf) VALUES (?,?,?,?,?)",
                       (rid, mes, versao, por, str(caminho))).lastrowid
    conn.executemany(
        "INSERT INTO fechamento_valores (fechamento_id, objeto_id, segundos_periodo, segundos_fora, segundos_expurgo, "
        "disponibilidade) VALUES (?,?,?,?,?,?)",
        [(fid, v["objeto_id"], v["segundos_periodo"], v["segundos_fora"], v["segundos_expurgo"], v["disponibilidade"])
         for v in valores])
    conn.commit()
    print(f"{rid} {mes} fechado: v{versao} → {caminho}")
    # o parcial diário deste relatório deixa de valer: sai da pasta de parciais
    pp = pasta_mes(mes, parciais=True)
    base = nome_arquivo(defn, mes)[:-4]           # sem ".pdf"
    for f in (pp.glob("*.pdf") if pp.exists() else []):
        if f.name == base + ".pdf" or f.name == base + " (parcial).pdf":
            f.unlink()
    if pp.exists() and not any(pp.iterdir()):
        pp.rmdir()
    return caminho


def parciais(conn, mes=None):
    """Gera o parcial do mês de todos os relatórios (substitui o do dia anterior)."""
    mes = mes or datetime.now(disp.TZ).strftime("%Y-%m")
    pasta = pasta_mes(mes, parciais=True)
    pasta.mkdir(parents=True, exist_ok=True)
    erros = 0
    for rid in definicoes():
        try:
            conteudo, nome, _ = gerar(conn, rid, mes)
            (pasta / nome).write_bytes(conteudo)
            print(f"{rid}: {pasta / nome}")
        except Exception as ex:  # noqa: BLE001
            erros += 1
            print(f"[!] {rid} {mes}: {ex}", file=sys.stderr)
    return erros


def mes_anterior():
    hoje = datetime.now(disp.TZ)
    ano, m = (hoje.year, hoje.month - 1) if hoje.month > 1 else (hoje.year - 1, 12)
    return f"{ano:04d}-{m:02d}"


def main():
    ap = argparse.ArgumentParser(description="Relatórios PDF de disponibilidade")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("gerar")
    s.add_argument("--relatorio")
    s.add_argument("--mes", default=None, help="AAAA-MM (padrão: mês atual)")
    s.add_argument("--de", help="período livre: primeiro dia AAAA-MM-DD (com --ate)")
    s.add_argument("--ate", help="período livre: último dia AAAA-MM-DD, incluído inteiro")
    s.add_argument("--todos", action="store_true", help="gera todos os relatórios (use --saida como pasta)")
    s.add_argument("--saida")
    s = sub.add_parser("fechar")
    s.add_argument("--relatorio", required=True)
    s.add_argument("--mes", required=True)
    s.add_argument("--por", default="sistema")
    s.add_argument("--nova-versao", action="store_true")
    sub.add_parser("fechar-mes-anterior")
    s = sub.add_parser("parciais")
    s.add_argument("--mes", default=None, help="AAAA-MM (padrão: mês atual)")
    a = ap.parse_args()

    conn = disp.conectar()
    if a.cmd == "gerar":
        mes = a.mes or datetime.now(disp.TZ).strftime("%Y-%m")
        if a.de or a.ate:
            if not (a.de and a.ate):
                raise SystemExit("use --de e --ate juntos")
            mes = f"{a.de}..{a.ate}"
        if a.todos:
            pasta = Path(a.saida or ".")
            pasta.mkdir(parents=True, exist_ok=True)
            for rid in definicoes():
                conteudo, nome, parcial = gerar(conn, rid, mes)
                (pasta / nome).write_bytes(conteudo)
                print(f"{pasta / nome}{' (parcial)' if parcial else ''}")
            return
        if not a.relatorio:
            raise SystemExit("informe --relatorio ou --todos")
        conteudo, nome, parcial = gerar(conn, a.relatorio, mes)
        destino = Path(a.saida or nome)
        destino.write_bytes(conteudo)
        print(f"{destino} ({'parcial' if parcial else 'período completo, não oficial'})")
    elif a.cmd == "fechar":
        fechar(conn, a.relatorio, a.mes, a.por, a.nova_versao)
    elif a.cmd == "fechar-mes-anterior":
        mes = mes_anterior()
        erros = 0
        for rid in definicoes():          # um relatório com erro não impede o fechamento dos outros
            try:
                fechar(conn, rid, mes, "sistema")
            except Exception as ex:  # noqa: BLE001
                erros += 1
                print(f"[!] {rid} {mes}: {ex}", file=sys.stderr)
        if erros:
            sys.exit(1)
    elif a.cmd == "parciais":
        if parciais(conn, a.mes):
            sys.exit(1)


if __name__ == "__main__":
    main()
