#!/usr/bin/env python3
"""
API HTTP somente leitura sobre o banco de disponibilidade (usa o mesmo cálculo do disp.py).
Pensada para o Grafana (datasource Infinity) e para baixar os PDFs.

    GET /saude
    GET /disponibilidade?relatorio=tid-usinas&mes=2026-10[&grupo=GERACAO][&prefixo=GA,NE][&por=grupo][&formato=powerplant|apos_on&cliente=ENGIE]
    GET /resumo?mes=2026-10&relatorios=tid-usinas,tid-aplicacoes[&unidade_grupo=tid-aplicacoes]
    GET /incidentes?relatorio=tid-usinas&mes=2026-10[&item=UHCC][&grupo=OI][&ordem=desc]
    GET /relatorios                                   (relatórios PDF definidos em relatorios.json)
    GET /relatorio.pdf?relatorio=tid[&mes=2026-10][&recalcular=1]
    GET /relatorio.pdf?relatorio=tid&de=2026-10-26&ate=2026-10-30      (período livre, dias inteiros)
        mês corrente = PDF parcial gerado na hora; mês fechado = PDF oficial guardado no fechamento

mes: AAAA-MM (padrão: mês atual), ou de=AAAA-MM-DD&ate=AAAA-MM-DD em qualquer rota (período livre). Percentuais já cortados em 2 casas (99.99 = 99,99%).

Variáveis: DISP_DB (banco), SERVIDOR_IP (onde atender; API_HOST tem prioridade; padrão 0.0.0.0), API_PORT (padrão 8099),
           API_TOKEN (opcional: exige header "Authorization: Bearer <token>")
"""
import json
import math
import os
import re
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import disp  # noqa: E402

TOKEN = os.getenv("API_TOKEN")


def pct(f):
    return None if f is None else math.floor(round(f * 10000, 6)) / 100


def chave(texto):
    return re.sub(r"[^A-Z0-9]+", "_", (texto or "SEM_GRUPO").upper()).strip("_")


def nome_powerplant(nome, cliente):
    sigla = nome.split(" - ")[0].split("_")[0].strip()
    return f"{cliente} - {sigla}_POWERPLANT"


def nome_exibicao(nome, formato, cliente):
    if formato == "powerplant":
        return nome_powerplant(nome, cliente)
    if formato == "apos_on":            # "LINK - MPLS EBT ON SD_UHSO_ADM" -> "SD_UHSO_ADM"
        return nome.split(" ON ", 1)[-1].strip()
    return nome


def mes_padrao():
    return datetime.now(disp.TZ).strftime("%Y-%m")


def media(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def filtrar_prefixo(linhas, prefixo):
    """prefixo='GA,NE' -> só itens que começam com GA ou NE (sem diferenciar maiúsculas)."""
    ps = tuple(p.strip().upper() for p in (prefixo or "").split(",") if p.strip())
    return [l for l in linhas if l["item"].upper().startswith(ps)] if ps else linhas


def grupo_filtro(q):
    """grupo vazio, '*' ou o 'All' do Grafana = sem filtro."""
    g = (q.get("grupo") or "").strip()
    return None if g in ("", "*", "$__all", "All") else g


def chamados_por_objeto(conn, rel, mes):
    """{objeto_id: nº de chamados distintos} das quedas que tocam o período."""
    m_ini, m_fim = disp.limites_mes(mes)
    out = {}
    for r in conn.execute(
            "SELECT i.id, i.objeto_id, i.chamado FROM incidentes i JOIN objetos o ON o.id = i.objeto_id "
            "WHERE o.relatorio = ? AND i.chamado IS NOT NULL AND i.inicio < ? AND coalesce(i.fim, ?) > ?",
            (rel, m_fim, int(time.time()), m_ini)):
        if disp.situacao_expurgo(conn, r["id"])[0] != "total":
            out.setdefault(r["objeto_id"], set()).add(r["chamado"])
    return {k: len(v) for k, v in out.items()}


def disponibilidade(conn, q):
    rel = q["relatorio"]
    mes = q.get("mes") or mes_padrao()
    ano, m = mes[:4], mes[5:7]
    linhas = filtrar_prefixo(disp.calcular(conn, rel, mes, grupo=grupo_filtro(q)), q.get("prefixo"))
    cham = chamados_por_objeto(conn, rel, mes)
    if q.get("por") == "grupo":
        grupos = {}
        for l in linhas:
            grupos.setdefault(l["grupo"], []).append(l)
        out = [{"NAME": g or "(sem grupo)", "DISPONIBILIDADE": pct(media([x["disponibilidade"] for x in ls])),
                "OBJETOS": len(ls), "SEGUNDOS_FORA": sum(x["segundos_fora"] for x in ls),
                "INCIDENTES": sum(x["incidentes"] for x in ls),
                "CHAMADOS": sum(cham.get(x["objeto_id"], 0) for x in ls)} for g, ls in grupos.items()]
    else:
        fmt = q.get("formato")
        cliente = q.get("cliente", "ENGIE")
        out = [{"NAME": nome_exibicao(l["nome"], fmt, cliente),
                "ITEM": l["item"], "GRUPO": l["grupo"], "DESCRICAO": l.get("descricao"),
                "DISPONIBILIDADE": pct(l["disponibilidade"]),
                "SEGUNDOS_FORA": l["segundos_fora"], "SEGUNDOS_EXPURGO": l["segundos_expurgo"],
                "INCIDENTES": l["incidentes"], "CHAMADOS": cham.get(l["objeto_id"], 0)} for l in linhas]
    for r in out:
        r["MES"], r["ANO"] = int(m), int(ano)
    out.sort(key=lambda r: (r["DISPONIBILIDADE"] is None, r["DISPONIBILIDADE"] or 0, r["NAME"]))
    return out


def resumo(conn, q):
    """Uma linha só: média por relatório, por grupo e GERAL.
    Em relatórios de 'unidade_grupo' a unidade é o grupo (ex.: aplicação = média dos componentes)."""
    mes = q.get("mes") or mes_padrao()
    ano, m = mes[:4], mes[5:7]
    por_grupo = set(filter(None, q.get("unidade_grupo", "").split(",")))
    linha, todas_unidades = {"MES": int(m), "ANO": int(ano)}, []
    for rel in filter(None, q.get("relatorios", "").split(",")):
        linhas = disp.calcular(conn, rel, mes)
        grupos = disp.agregar(linhas)
        if rel in por_grupo:
            unidades = list(grupos.values())
        else:
            unidades = [l["disponibilidade"] for l in linhas]
            for g, v in grupos.items():
                linha[chave(g)] = pct(v)
        linha[chave(rel)] = pct(media(unidades))
        todas_unidades += unidades
    linha["GERAL"] = pct(media(todas_unidades))
    return [linha]


def incidentes(conn, q):
    rel, mes = q["relatorio"], q.get("mes") or mes_padrao()
    m_ini, m_fim = disp.limites_mes(mes)
    agora = int(time.time())
    sql = ("SELECT i.id, o.item, o.grupo, i.inicio, i.fim, i.problema, i.chamado, i.chamado_externo, i.causa "
           "FROM incidentes i JOIN objetos o ON o.id = i.objeto_id WHERE o.relatorio = ? AND i.inicio < ? "
           "AND coalesce(i.fim, ?) > ?")
    args = [rel, m_fim, agora, m_ini]
    if q.get("item"):
        sql += " AND o.item LIKE ?"
        args.append(f"%{q['item']}%")
    if grupo_filtro(q):
        sql += " AND o.grupo = ?"
        args.append(grupo_filtro(q))
    ordem = "DESC" if q.get("ordem") == "desc" else "ASC"
    fmt = lambda t: datetime.fromtimestamp(t, disp.TZ).strftime("%d/%m/%Y %H:%M:%S") if t else None
    out = []
    for r in conn.execute(sql + f" ORDER BY i.inicio {ordem}", args):
        cob, _ = disp.situacao_expurgo(conn, r["id"])
        if cob == "total" and q.get("expurgados") == "0":
            continue                    # expurgados=0 esconde as quedas desconsideradas por inteiro
        motivo = disp.descricao_expurgo(conn, r["id"]) if cob else None
        out.append({"ID": r["id"], "ITEM": r["item"], "GRUPO": r["grupo"], "INICIO": fmt(r["inicio"]),
                    "FIM": fmt(r["fim"]), "MINUTOS": int(((r["fim"] or agora) - r["inicio"]) // 60),
                    "PROBLEMA": r["problema"], "CHAMADO": r["chamado"], "CHAMADO_EXTERNO": r["chamado_externo"],
                    "CAUSA": r["causa"], "EXPURGO": cob, "MOTIVO_EXPURGO": motivo})
    return out


def relatorios_definidos(conn, q):
    import relatorio
    return [{"ID": k, "TITULO": v["titulo"], "CLIENTE": v.get("cliente")} for k, v in relatorio.definicoes().items()]


ROTAS = {"/disponibilidade": disponibilidade, "/resumo": resumo, "/incidentes": incidentes,
         "/relatorios": relatorios_definidos}


class Handler(BaseHTTPRequestHandler):
    def responder(self, codigo, corpo):
        b = json.dumps(corpo, ensure_ascii=False).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def responder_pdf(self, conteudo, nome):
        from urllib.parse import quote
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(nome)}")
        self.send_header("Content-Length", str(len(conteudo)))
        self.end_headers()
        self.wfile.write(conteudo)

    def pdf(self, q):
        import relatorio
        rid = q["relatorio"]
        mes = q.get("mes") or mes_padrao()
        if q.get("de") or q.get("ate"):          # período livre: de=AAAA-MM-DD&ate=AAAA-MM-DD
            if not all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", q.get(k) or "") for k in ("de", "ate")):
                raise ValueError("de e ate devem ser AAAA-MM-DD")
            mes = f"{q['de']}..{q['ate']}"
        conn = disp.conectar()
        try:
            oficial = None if (q.get("recalcular") or disp.periodo_livre(mes)) else relatorio.pdf_oficial(conn, rid, mes)
            if oficial:
                return self.responder_pdf(oficial.read_bytes(), oficial.name)
            conteudo, nome, _ = relatorio.gerar(conn, rid, mes)
            self.responder_pdf(conteudo, nome)
        finally:
            conn.close()

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/saude":
            return self.responder(200, {"ok": True})
        if TOKEN and self.headers.get("Authorization") != f"Bearer {TOKEN}":
            return self.responder(401, {"erro": "não autorizado"})
        q = {k: v[-1] for k, v in parse_qs(u.query).items()}
        if u.path == "/relatorio.pdf":
            try:
                if q.get("mes") and not re.fullmatch(r"\d{4}-\d{2}", q["mes"]):
                    raise ValueError("mes deve ser AAAA-MM")
                return self.pdf(q)
            except KeyError as e:
                return self.responder(400, {"erro": f"parâmetro/relatório inválido: {e.args[0]}"})
            except Exception as e:  # noqa: BLE001
                return self.responder(500, {"erro": str(e)})
        rota = ROTAS.get(u.path)
        if not rota:
            return self.responder(404, {"erro": "rota inexistente", "rotas": sorted(ROTAS) + ["/relatorio.pdf"]})
        try:
            if q.get("mes") and not re.fullmatch(r"\d{4}-\d{2}", q["mes"]):
                raise ValueError("mes deve ser AAAA-MM")
            if q.get("de") and q.get("ate"):         # período livre (ex.: seletor de tempo do Grafana)
                if not all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", q[k]) for k in ("de", "ate")):
                    raise ValueError("de e ate devem ser AAAA-MM-DD")
                q["mes"] = f"{q['de']}..{q['ate']}"
            conn = disp.conectar()
            try:
                self.responder(200, rota(conn, q))
            finally:
                conn.close()
        except KeyError as e:
            self.responder(400, {"erro": f"parâmetro obrigatório: {e.args[0]}"})
        except Exception as e:  # noqa: BLE001
            self.responder(500, {"erro": str(e)})

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.address_string()} {fmt % args}\n")


if __name__ == "__main__":
    host = os.getenv("API_HOST") or os.getenv("SERVIDOR_IP") or "0.0.0.0"
    porta = int(os.getenv("API_PORT", "8099"))
    print(f"API de disponibilidade em http://{host}:{porta} (banco: {os.getenv('DISP_DB', 'disponibilidade.db')})")
    ThreadingHTTPServer((host, porta), Handler).serve_forever()
