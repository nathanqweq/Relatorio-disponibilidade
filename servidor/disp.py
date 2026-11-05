#!/usr/bin/env python3
"""
Banco e motor de disponibilidade por eventos (SQLite).

Regra de cálculo, por objeto e por mês (fuso America/Sao_Paulo):
    janela   = mês ∩ [ativo_desde, ativo_ate] ∩ [.., agora]
    base     = janela − lacunas (períodos sem monitoramento)
    fora     = união dos incidentes ∩ base          (sobreposições contam uma vez)
    expurgo  = união dos expurgos APROVADOS ∩ fora
    disp     = 1 − (fora − expurgo) / base
O percentual nunca é gravado/alterado: é sempre recalculado a partir dos fatos.

Comandos
    python3 disp.py init
    python3 disp.py sync-objetos     [--desde AAAA-MM-DD]
    python3 disp.py importar-eventos --de AAAA-MM-DD --ate AAAA-MM-DD [--margem-dias 7]
    python3 disp.py calcular         --relatorio TID --mes 2026-07 [--json]
    python3 disp.py incidentes       --relatorio TID --mes 2026-07 [--item UHCC]
    python3 disp.py expurgo-propor   --objeto ... --inicio ... --fim ... --tipo ... --motivo ... --por ...
    python3 disp.py expurgo-decidir  --id 3 --status aprovado --por nome
    python3 disp.py desconsiderar    --chamado I2610-123 --tipo falso_positivo --motivo "..." --por nome
    python3 disp.py desconsiderar    --incidente 812 [--inicio "2026-10-05 10:00" --fim "2026-10-05 10:30"] ...
    python3 disp.py contabilizar     --chamado I2610-123 --por nome      (desfaz)
    python3 disp.py expurgo-incidente --incidente 123 --tipo falso_positivo --motivo ... --por nome [--aprovar]
    python3 disp.py expurgos         [--relatorio X] [--mes 2026-11] [--status aprovado]
    python3 disp.py expurgo-revogar  --id 3 --por nome
    python3 disp.py lacuna           --inicio ... --fim ... --motivo ... --por ... [--relatorio X]
    python3 disp.py limpar           [--meses 3] [--incluir-fechamentos]
    python3 disp.py remover-item     --id 12 --por nome [--incluir-fechados]   (item encerrado: apaga de vez)
    python3 disp.py importar-chamados [--dias 45] [--chamado I2610-123]   (TOPdesk: ações dos analistas)
    python3 disp.py chamado I2610-123

Banco: variável DISP_DB (padrão ./disponibilidade.db)
Zabbix: ZABBIX_URL e ZABBIX_TOKEN
TOPdesk: TOPDESK_URL, TOPDESK_USER, TOPDESK_APP_PASSWORD [, TOPDESK_VERIFY_TLS=0]
"""
import argparse
import json
import math
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Sao_Paulo")
SCHEMA = Path(__file__).with_name("schema.sql")
TAG_RELATORIO, TAG_ITEM, TAG_GRUPO = "relatorio", "item", "grupo"

# Número do chamado no comentário do problema (acknowledge).
# 1º "chamado: X" / "ticket X" / "incidente nº X"; 2º padrão de numeração do TOPdesk (I2610-123, I 2610 123).
# Para outro formato: export CHAMADO_REGEX='...'  (o 1º grupo da regex é o número)
CHAMADO_REGEXES = [os.getenv("CHAMADO_REGEX")] if os.getenv("CHAMADO_REGEX") else [
    r"(?i)chamado\s+(?:criado|aberto|encerrado|fechado)\s*:\s*([A-Z]?\d{4}-\d+)",   # godesk: "Chamado criado: I2610-12878"
    r"(?i)(?:chamado|ticket|incidente|incident)\s*(?:n[º°o.]*\s*)?[:#-]?\s*([A-Z]{0,3}\s?\d[\w-]*(?:\s\d{2,6})?)",
    r"\b([IPC]\s?\d{4}[\s-]?\d{2,6})\b",
]


# ------------------------------------------------------------ banco ----

def conectar(caminho=None):
    caminho = caminho or os.getenv("DISP_DB", "disponibilidade.db")
    conn = sqlite3.connect(caminho, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


# ---------------------------------------------------- intervalos ----
# Intervalo = (inicio, fim) em epoch, semiaberto [inicio, fim)

def unir(intervalos):
    out = []
    for a, b in sorted(i for i in intervalos if i[1] > i[0]):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def intersectar(xs, ys):
    """Interseção de duas listas de intervalos já unidas."""
    out, i, j = [], 0, 0
    while i < len(xs) and j < len(ys):
        a, b = max(xs[i][0], ys[j][0]), min(xs[i][1], ys[j][1])
        if a < b:
            out.append((a, b))
        if xs[i][1] < ys[j][1]:
            i += 1
        else:
            j += 1
    return out


def subtrair(xs, ys):
    """xs − ys (listas unidas)."""
    out = []
    for a, b in xs:
        cur = a
        for c, d in ys:
            if d <= cur or c >= b:
                continue
            if c > cur:
                out.append((cur, c))
            cur = max(cur, d)
            if cur >= b:
                break
        if cur < b:
            out.append((cur, b))
    return out


def total(xs):
    return sum(b - a for a, b in xs)


# ------------------------------------------------------- período ----

def periodo_livre(mes):
    """'2026-10-26..2026-10-30' -> ('2026-10-26', '2026-10-30'); mês normal -> None."""
    return tuple(mes.split("..", 1)) if ".." in (mes or "") else None


def limites_mes(mes):
    """Mês 'AAAA-MM' ou período livre 'AAAA-MM-DD..AAAA-MM-DD' (de 00:00 do 1º dia a 23:59:59 do último)."""
    p = periodo_livre(mes)
    if p:
        ini, fim = epoch(p[0]), epoch(p[1]) + 86400
        if fim <= ini:
            raise SystemExit("período inválido: a data final é anterior à inicial")
        return ini, fim
    ano, m = map(int, mes.split("-"))
    ini = datetime(ano, m, 1, tzinfo=TZ)
    fim = datetime(ano + (m == 12), m % 12 + 1, 1, tzinfo=TZ)
    return int(ini.timestamp()), int(fim.timestamp())


def epoch(texto):
    """'2026-07-01' ou '2026-07-01 13:45[:00]' no fuso de Brasília -> epoch."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(texto, fmt).replace(tzinfo=TZ).timestamp())
        except ValueError:
            pass
    raise SystemExit(f"Data inválida: {texto}")


# -------------------------------------------------------- cálculo ----

def calcular(conn, relatorio, mes, agora=None, grupo=None):
    """Lista de dicts por objeto do relatório no mês."""
    agora = int(agora or time.time())
    m_ini, m_fim = limites_mes(mes)
    lac = unir([(r["inicio"], r["fim"]) for r in conn.execute(
        "SELECT inicio, fim FROM lacunas WHERE (relatorio IS NULL OR relatorio = ?) AND fim > ? AND inicio < ?",
        (relatorio, m_ini, m_fim))])
    exp_rel = [(r["inicio"], r["fim"]) for r in conn.execute(
        "SELECT inicio, fim FROM expurgos WHERE status='aprovado' AND objeto_id IS NULL AND relatorio = ? "
        "AND fim > ? AND inicio < ?", (relatorio, m_ini, m_fim))]

    sql = "SELECT * FROM objetos WHERE relatorio = ?"
    args = [relatorio]
    if grupo:
        sql += " AND grupo = ?"
        args.append(grupo)
    resultado = []
    for o in conn.execute(sql + " ORDER BY grupo, nome, item", args):
        if (o["ativo_ate"] is not None and o["ativo_ate"] <= m_ini) or o["ativo_desde"] >= m_fim:
            continue                    # removido antes do mês ou criado depois: fora do relatório
        ini = max(m_ini, o["ativo_desde"])
        fim = min(m_fim, o["ativo_ate"] or m_fim, agora)
        janela = [(ini, fim)] if fim > ini else []
        base = subtrair(janela, lac)
        incs = conn.execute(
            "SELECT id, inicio, coalesce(fim, ?) AS fim FROM incidentes "
            "WHERE objeto_id = ? AND inicio < ? AND coalesce(fim, ?) > ?",
            (agora, o["id"], m_fim, agora, m_ini)).fetchall()
        fora = intersectar(unir([(r["inicio"], r["fim"]) for r in incs]), base)
        exps = [(r["inicio"], r["fim"]) for r in conn.execute(
            "SELECT inicio, fim FROM expurgos WHERE status='aprovado' AND objeto_id = ? AND fim > ? AND inicio < ?",
            (o["id"], m_ini, m_fim))]
        exp_uni = unir(exps + exp_rel)
        expurgo = intersectar(exp_uni, fora)
        s_base, s_fora, s_exp = total(base), total(fora), total(expurgo)
        quedas = sum(1 for r in incs if total(subtrair(intersectar([(r["inicio"], r["fim"])], base), exp_uni)) > 0)
        resultado.append({
            "objeto_id": o["id"], "item": o["item"], "nome": o["nome"] or o["item"],
            "grupo": o["grupo"], "descricao": o["descricao"],
            "segundos_periodo": s_base, "segundos_fora": s_fora, "segundos_expurgo": s_exp,
            "segundos_lacuna": total(janela) - s_base, "incidentes": quedas,
            "disponibilidade": None if s_base == 0 else 1 - (s_fora - s_exp) / s_base,
        })
    return resultado


TIPOS_EXPURGO = {
    "falso_positivo": "Falso positivo",
    "solicitacao_cliente": "Ajuste solicitado pelo cliente",
    "manutencao_programada": "Manutenção programada",
    "desligamento_nao_comunicado": "Desligamento não comunicado",
    "energia_concessionaria": "Falta de energia da concessionária",
    "outro": "Outro",
}


def descricao_expurgo(conn, incidente_id, agora=None):
    """Texto para o relatório: 'Falso positivo: alarme falso (por Ana)'; None se a queda não tem expurgo."""
    agora = int(agora or time.time())
    i = conn.execute("SELECT i.objeto_id, i.inicio, coalesce(i.fim, ?) AS fim, o.relatorio FROM incidentes i "
                     "JOIN objetos o ON o.id = i.objeto_id WHERE i.id = ?", (agora, incidente_id)).fetchone()
    if not i:
        return None
    partes = []
    for e in conn.execute(
            "SELECT tipo, motivo, coalesce(decidido_por, proposto_por) AS por FROM expurgos WHERE status='aprovado' "
            "AND fim > ? AND inicio < ? AND (objeto_id = ? OR (objeto_id IS NULL AND relatorio = ?)) ORDER BY id",
            (i["inicio"], i["fim"], i["objeto_id"], i["relatorio"])):
        partes.append(f"{TIPOS_EXPURGO.get(e['tipo'], e['tipo'])}: {e['motivo']} (por {e['por']})")
    return "; ".join(dict.fromkeys(partes)) or None


def desconsiderar(conn, incidente_ids, tipo, motivo, por, inicio=None, fim=None):
    """Cria expurgos JÁ APROVADOS para as quedas (inteiras, ou só o trecho inicio–fim).
    A queda continua no relatório, marcada como não contabilizada. Não duplica expurgo igual."""
    agora = int(time.time())
    criados = []
    for iid in incidente_ids:
        i = conn.execute("SELECT i.*, o.item, o.relatorio FROM incidentes i JOIN objetos o ON o.id = i.objeto_id "
                         "WHERE i.id = ?", (iid,)).fetchone()
        if not i:
            raise SystemExit(f"incidente {iid} não encontrado (veja o id com: disp.py incidentes ...)")
        ini = inicio or i["inicio"]
        fi = fim or (i["fim"] or agora)
        if fi <= ini:
            raise SystemExit(f"incidente {iid}: o trecho informado não tem duração")
        if conn.execute("SELECT 1 FROM expurgos WHERE incidente_id=? AND status='aprovado' AND inicio=? AND fim=?",
                        (iid, ini, fi)).fetchone():
            continue
        cur = conn.execute(
            "INSERT INTO expurgos (objeto_id, incidente_id, inicio, fim, tipo, motivo, chamado, proposto_por, "
            "status, decidido_por, decidido_em) VALUES (?,?,?,?,?,?,?,?, 'aprovado', ?, strftime('%s','now'))",
            (i["objeto_id"], iid, ini, fi, tipo, motivo, i["chamado"], por, por))
        criados.append((cur.lastrowid, i["relatorio"], i["item"], ini, fi))
    conn.commit()
    return criados


def situacao_expurgo(conn, incidente_id, agora=None):
    """('total' | 'parcial' | None, motivo) conforme os expurgos APROVADOS que cobrem a queda."""
    agora = int(agora or time.time())
    i = conn.execute("SELECT i.objeto_id, i.inicio, coalesce(i.fim, ?) AS fim, o.relatorio FROM incidentes i "
                     "JOIN objetos o ON o.id = i.objeto_id WHERE i.id = ?", (agora, incidente_id)).fetchone()
    if not i:
        return None, None
    exps = conn.execute(
        "SELECT inicio, fim, motivo FROM expurgos WHERE status='aprovado' AND fim > ? AND inicio < ? "
        "AND (objeto_id = ? OR (objeto_id IS NULL AND relatorio = ?)) ORDER BY id",
        (i["inicio"], i["fim"], i["objeto_id"], i["relatorio"])).fetchall()
    if not exps:
        return None, None
    coberto = total(intersectar([(i["inicio"], i["fim"])], unir([(e["inicio"], e["fim"]) for e in exps])))
    motivo = "; ".join(dict.fromkeys(e["motivo"] for e in exps))
    return ("total" if coberto >= i["fim"] - i["inicio"] else "parcial"), motivo


def agregar(linhas, chave="grupo"):
    """Média simples dos objetos com dado, por grupo (mesma regra do relatório atual)."""
    grupos = {}
    for l in linhas:
        if l["disponibilidade"] is not None:
            grupos.setdefault(l[chave], []).append(l["disponibilidade"])
    return {g: sum(v) / len(v) for g, v in grupos.items()}


def pct(f):
    """Corta (não arredonda) em 2 casas: qualquer queda aparece como no máximo 99,99%."""
    if f is None:
        return "—"
    v = math.floor(round(f * 10000, 6)) / 100
    return f"{v:.2f}".replace(".", ",") + "%"


def dur(s):
    h, r = divmod(int(s), 3600)
    return f"{h}h{r // 60:02d}"


# ---------------------------------------------------- Zabbix API ----

class Zabbix:
    def __init__(self):
        import requests
        url, token = os.getenv("ZABBIX_URL"), os.getenv("ZABBIX_TOKEN")
        if not url or not token:
            raise SystemExit("Defina ZABBIX_URL e ZABBIX_TOKEN")
        self.url = url.rstrip("/") + ("" if url.endswith("api_jsonrpc.php") else "/api_jsonrpc.php")
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json-rpc"})
        self.s.verify = os.getenv("ZABBIX_VERIFY_TLS", "1") != "0"
        if not self.s.verify:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def call(self, method, params):
        r = self.s.post(self.url, json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1}, timeout=120)
        r.raise_for_status()
        d = r.json()
        if "error" in d:
            raise RuntimeError(f"{method}: {d['error'].get('message')} {d['error'].get('data')}")
        return d["result"]


def tags_por_nome(tags):
    out = {}
    for t in tags or []:
        out.setdefault(t["tag"], []).append(t.get("value", ""))
    return out


def expandir(valor, host):
    """Resolve as macros usadas em tags de template: {HOST.NAME}, {HOST.HOST} e macros de usuário
    ({$LINK.OPERADORA}) já resolvidas para o host em host["macros"] (host > templates > global)."""
    v = (valor or "").replace("{HOST.NAME}", host.get("name", "")).replace("{HOST.HOST}", host.get("host", ""))
    macros = host.get("macros") or {}
    return re.sub(r"\{\$[A-Z0-9_.]+\}", lambda m: macros.get(m.group(0), m.group(0)), v)


def macros_resolvidas(zbx, hostids):
    """{hostid: {"{$MACRO}": valor}} com a herança do Zabbix: host > templates (em profundidade) > global."""
    globais = {m["macro"]: m["value"] for m in zbx.call("usermacro.get", {"output": ["macro", "value"],
                                                                          "globalmacro": True})}
    hosts = zbx.call("host.get", {"output": ["hostid"], "hostids": hostids, "selectMacros": ["macro", "value"],
                                  "selectParentTemplates": ["templateid"]})
    templates, pendentes = {}, {t["templateid"] for h in hosts for t in h["parentTemplates"]}
    while pendentes:
        lote = zbx.call("template.get", {"output": ["templateid"], "templateids": sorted(pendentes),
                                         "selectMacros": ["macro", "value"], "selectParentTemplates": ["templateid"]})
        for t in lote:
            templates[t["templateid"]] = t
        pendentes = {p["templateid"] for t in lote for p in t["parentTemplates"]} - set(templates)

    def do_template(tid, vistos):
        if tid in vistos or tid not in templates:
            return {}
        vistos.add(tid)
        t, out = templates[tid], {}
        for p in reversed(t["parentTemplates"]):
            out.update(do_template(p["templateid"], vistos))
        out.update({m["macro"]: m["value"] for m in t.get("macros", [])})
        return out

    res = {}
    for h in hosts:
        m = dict(globais)
        for t in reversed(h["parentTemplates"]):
            m.update(do_template(t["templateid"], set()))
        m.update({x["macro"]: x["value"] for x in h.get("macros", [])})
        res[h["hostid"]] = m
    return res


def chaves_objeto(tags, host):
    """Uma trigger/evento pode entrar em mais de um relatório (várias tags relatorio).
    host = {"name": ..., "host": ..., "tags": [...]}
    'item' e 'grupo' vêm da trigger; sem 'grupo' na trigger, vale a tag 'grupo' do host
    (ex.: links: trigger do template com relatorio/item, operadora como tag grupo no host)."""
    tags = [{"tag": t["tag"], "value": expandir(t.get("value", ""), host)} for t in tags or []]
    t = tags_por_nome(tags)
    if not t.get(TAG_GRUPO):
        do_host = tags_por_nome(host.get("tags")).get(TAG_GRUPO)
        if do_host:
            t[TAG_GRUPO] = do_host
    item = (t.get(TAG_ITEM) or [host.get("name", "")])[0]
    grupo = (t.get(TAG_GRUPO) or [None])[0]
    return [(rel, item, grupo) for rel in t.get(TAG_RELATORIO, []) if rel], t


def descricao_por_tags(tags):
    """Sem descrição no host: monta a partir das tags DESIGNACAO / VELOCIDADE / LOCALIDADE (links)."""
    t = {k.upper(): v[0].strip() for k, v in tags_por_nome(tags).items() if v and v[0].strip()}
    partes = [t.get("DESIGNACAO")]
    if t.get("VELOCIDADE"):
        partes.append(t["VELOCIDADE"] + (" Mbps" if t["VELOCIDADE"].isdigit() else ""))
    partes.append(t.get("LOCALIDADE"))
    return " · ".join(p for p in partes if p) or None


def desativar_ausentes(conn, vistos, agora=None):
    """Objetos ativos que o sync não encontrou mais no Zabbix (template/tag removidos) recebem ativo_ate.
    Saem dos meses seguintes e continuam nos meses em que existiam."""
    agora = int(agora or time.time())
    saiu = [(r["id"], r["relatorio"], r["item"]) for r in conn.execute(
        "SELECT id, relatorio, item FROM objetos WHERE ativo_ate IS NULL")
        if r["id"] not in vistos]
    conn.executemany("UPDATE objetos SET ativo_ate=? WHERE id=?", [(agora, i) for i, _, _ in saiu])
    return [(rel, item) for _, rel, item in saiu]


def sync_objetos(conn, desde):
    zbx = Zabbix()
    trigs = zbx.call("trigger.get", {
        "output": ["triggerid", "description", "status"], "expandDescription": True,
        "selectTags": "extend", "selectHosts": ["hostid", "host", "name"],
        "tags": [{"tag": TAG_RELATORIO, "operator": 4}],  # 4 = existe
        "templated": False,                               # só triggers de hosts (inclui as herdadas de template)
    })
    # tags e descrição dos hosts (operadora na tag 'grupo' do host; designador/contato na descrição)
    hostids = sorted({h["hostid"] for tr in trigs for h in tr.get("hosts", [])})
    hosts = {}
    for i in range(0, len(hostids), 1000):
        for h in zbx.call("host.get", {"output": ["hostid", "description"], "hostids": hostids[i:i + 1000],
                                       "selectTags": "extend"}):
            hosts[h["hostid"]] = h
    macros = {}
    for i in range(0, len(hostids), 1000):
        macros.update(macros_resolvidas(zbx, hostids[i:i + 1000]))
    novos, vistos = 0, set()
    for tr in trigs:
        host = dict((tr.get("hosts") or [{}])[0])
        extra = hosts.get(host.get("hostid"), {})
        host["macros"] = macros.get(host.get("hostid"), {})
        host["tags"] = [{"tag": t["tag"], "value": expandir(t.get("value", ""), host)} for t in extra.get("tags", [])]
        descricao = (extra.get("description") or "").strip() or descricao_por_tags(host["tags"])
        chaves, t = chaves_objeto(tr["tags"], host)
        for rel, item, grupo in chaves:
            cur = conn.execute(
                "INSERT INTO objetos (relatorio, item, grupo, nome, descricao, tags_json, zabbix_triggerid, ativo_desde) "
                "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(relatorio, item) DO UPDATE SET "
                "grupo=excluded.grupo, descricao=excluded.descricao, tags_json=excluded.tags_json, "
                "zabbix_triggerid=excluded.zabbix_triggerid, ativo_ate=NULL",
                (rel, item, grupo, item, descricao, json.dumps(t, ensure_ascii=False), tr["triggerid"], desde))
            novos += cur.rowcount
            oid = conn.execute("SELECT id FROM objetos WHERE relatorio=? AND item=?", (rel, item)).fetchone()["id"]
            vistos.add(oid)
            conn.execute("INSERT OR IGNORE INTO objeto_triggers (objeto_id, triggerid) VALUES (?,?)",
                         (oid, tr["triggerid"]))
    saiu = desativar_ausentes(conn, vistos) if trigs else []
    conn.commit()
    print(f"{len(trigs)} trigger(s) com tag '{TAG_RELATORIO}'; objetos criados/atualizados: {novos}")
    for rel, item in saiu:
        print(f"  encerrado (sem trigger no Zabbix): {rel} / {item}")


def extrair_chamado(acks):
    """Primeiro número de chamado encontrado nos comentários do evento (do mais antigo ao mais novo)."""
    for ack in sorted(acks or [], key=lambda a: int(a.get("clock", 0))):
        msg = ack.get("message") or ""
        for rx in CHAMADO_REGEXES:
            m = re.search(rx, msg)
            if m:
                return m.group(1).strip()
    return None


def gravar_incidente(conn, rel, item, eventid, r_eventid, inicio, fim, severidade, problema, **extra):
    o = conn.execute("SELECT id FROM objetos WHERE relatorio=? AND item=?", (rel, item)).fetchone()
    if not o:
        print(f"[!] objeto não cadastrado ({rel} / {item}); rode sync-objetos", file=sys.stderr)
        return None
    campos = {k: v for k, v in extra.items() if k in ("chamado", "chamado_externo", "causa", "resumo") and v}
    conn.execute(
        "INSERT INTO incidentes (objeto_id, zabbix_eventid, zabbix_r_eventid, inicio, fim, severidade, problema) "
        "VALUES (?,?,?,?,?,?,?) ON CONFLICT(zabbix_eventid) DO UPDATE SET "
        "zabbix_r_eventid=coalesce(excluded.zabbix_r_eventid, zabbix_r_eventid), "
        "fim=coalesce(excluded.fim, fim), atualizado_em=strftime('%s','now')",
        (o["id"], f"{eventid}:{rel}", r_eventid, inicio, fim, severidade, problema))
    if campos:
        sets = ", ".join(f"{k}=?" for k in campos)
        conn.execute(f"UPDATE incidentes SET {sets}, atualizado_em=strftime('%s','now') WHERE zabbix_eventid=?",
                     (*campos.values(), f"{eventid}:{rel}"))
    return conn.execute("SELECT id FROM incidentes WHERE zabbix_eventid=?", (f"{eventid}:{rel}",)).fetchone()["id"]


def gravar_comentarios(conn, incidente_id, acks):
    """Guarda os comentários do problema (acknowledges) sem duplicar."""
    for a in acks or []:
        msg = (a.get("message") or "").strip()
        if not msg:
            continue
        usuario = a.get("username") or " ".join(x for x in (a.get("name"), a.get("surname")) if x) or None
        conn.execute("INSERT OR IGNORE INTO comentarios (incidente_id, clock, usuario, mensagem) VALUES (?,?,?,?)",
                     (incidente_id, int(a.get("clock", 0)), usuario, msg))


def importar_eventos(conn, de, ate, margem_dias):
    """Importa os problemas das triggers cadastradas (sync-objetos) no período.
    Busca pela TRIGGER e não pela tag do evento: o Zabbix copia as tags no momento do
    problema, então eventos antigos não têm a tag 'relatorio' colocada depois.
    A margem pega problemas que começaram antes do período e ainda estavam abertos nele."""
    zbx = Zabbix()
    mapa = {}
    for r in conn.execute("SELECT ot.triggerid, o.relatorio, o.item FROM objeto_triggers ot "
                          "JOIN objetos o ON o.id = ot.objeto_id"):
        mapa.setdefault(r["triggerid"], []).append((r["relatorio"], r["item"]))
    if not mapa:
        raise SystemExit("Nenhuma trigger cadastrada: rode sync-objetos antes")
    t_de, t_ate = epoch(de) - margem_dias * 86400, epoch(ate) + 86400
    triggerids, eventos = list(mapa), []
    for i in range(0, len(triggerids), 500):
        ultimo = None
        while True:
            p = {"output": ["eventid", "clock", "r_eventid", "severity", "name", "objectid"],
                 "selectAcknowledges": "extend",
                 "source": 0, "object": 0, "value": 1, "objectids": triggerids[i:i + 500],
                 "time_from": t_de, "time_till": t_ate,
                 "sortfield": ["eventid"], "sortorder": "ASC", "limit": 5000}
            if ultimo:
                p["eventid_from"] = str(int(ultimo) + 1)
            parte = zbx.call("event.get", p)
            eventos += parte
            if len(parte) < 5000:
                break
            ultimo = parte[-1]["eventid"]
    rids = [e["r_eventid"] for e in eventos if e.get("r_eventid") not in (None, "0")]
    fins = {}
    for i in range(0, len(rids), 5000):
        for r in zbx.call("event.get", {"output": ["eventid", "clock"], "eventids": rids[i:i + 5000]}):
            fins[r["eventid"]] = int(r["clock"])
    gravados = com_chamado = 0
    for e in eventos:
        r = e.get("r_eventid")
        chamado = extrair_chamado(e.get("acknowledges"))
        com_chamado += bool(chamado)
        for rel, item in mapa.get(e["objectid"], []):
            iid = gravar_incidente(conn, rel, item, e["eventid"], r if r not in (None, "0") else None,
                                   int(e["clock"]), fins.get(r), int(e["severity"]), e["name"], chamado=chamado)
            if iid:
                gravados += 1
                gravar_comentarios(conn, iid, e.get("acknowledges"))
    conn.commit()
    print(f"{len(eventos)} evento(s) lidos de {len(triggerids)} trigger(s); "
          f"{gravados} incidente(s) gravados/atualizados; {com_chamado} com número de chamado")


# ------------------------------------------------------- TOPdesk ----

class TopDesk:
    """API REST do TOPdesk (usuário operador + senha de aplicação)."""
    def __init__(self):
        import requests
        url, user, senha = os.getenv("TOPDESK_URL"), os.getenv("TOPDESK_USER"), os.getenv("TOPDESK_APP_PASSWORD")
        if not url or not user or not senha:
            raise SystemExit("Defina TOPDESK_URL, TOPDESK_USER e TOPDESK_APP_PASSWORD")
        self.url = url.rstrip("/")
        self.s = requests.Session()
        self.s.auth = (user, senha)
        self.s.headers.update({"Accept": "application/json"})
        self.s.verify = os.getenv("TOPDESK_VERIFY_TLS", "1") != "0"
        if not self.s.verify:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def get(self, caminho, **params):
        r = self.s.get(f"{self.url}/tas/api/{caminho}", params=params, timeout=60)
        if r.status_code in (204, 404):
            return None
        r.raise_for_status()
        return r.json()

    def incidente(self, numero):
        return self.get(f"incidents/number/{numero}")

    def acoes(self, numero):
        out, inicio = [], 0
        while True:
            parte = self.get(f"incidents/number/{numero}/actions", start=inicio, page_size=100) or []
            out += parte
            if len(parte) < 100:
                return out
            inicio += 100


def data_topdesk(v):
    """'2026-10-20T13:45:00.000+0000' -> epoch."""
    if not v:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return int(datetime.strptime(v, fmt).timestamp())
        except ValueError:
            pass
    return None


def texto_simples(html):
    """memoText do TOPdesk vem em HTML simples: vira texto com quebras de linha."""
    import html as h
    t = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", html or "")
    t = h.unescape(re.sub(r"<[^>]+>", "", t))
    return "\n".join(l.strip() for l in t.splitlines() if l.strip())


def gravar_chamado(conn, numero, inc, acoes):
    ps = inc.get("processingStatus") or {}
    encerrado = 1 if inc.get("closed") or inc.get("completed") else 0
    conn.execute(
        "INSERT INTO chamados (numero, topdesk_id, titulo, status, encerrado, aberto_em, encerrado_em, atualizado_em) "
        "VALUES (?,?,?,?,?,?,?,strftime('%s','now')) ON CONFLICT(numero) DO UPDATE SET topdesk_id=excluded.topdesk_id, "
        "titulo=excluded.titulo, status=excluded.status, encerrado=excluded.encerrado, aberto_em=excluded.aberto_em, "
        "encerrado_em=excluded.encerrado_em, atualizado_em=excluded.atualizado_em",
        (numero, inc.get("id"), inc.get("briefDescription"), ps.get("name") if isinstance(ps, dict) else ps,
         encerrado, data_topdesk(inc.get("callDate") or inc.get("creationDate")),
         data_topdesk(inc.get("closedDate") or inc.get("completedDate"))))
    for a in acoes:
        operador = (a.get("operator") or {}).get("name")
        pessoa = (a.get("person") or {}).get("name")
        texto = (a.get("plainText") or "").strip() or texto_simples(a.get("memoText"))
        conn.execute(
            "INSERT INTO chamado_acoes (id, chamado, clock, autor, analista, texto) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET texto=excluded.texto, autor=excluded.autor",
            (a.get("id") or f"{numero}:{a.get('entryDate')}", numero, data_topdesk(a.get("entryDate")),
             operador or pessoa, 1 if operador else 0, texto))


def importar_chamados(conn, dias=45, numeros=None):
    """Busca no TOPdesk os chamados dos incidentes recentes. Chamado encerrado e já lido depois
    do encerramento não é consultado de novo."""
    td = TopDesk()
    if numeros is None:
        desde = int(time.time()) - dias * 86400
        numeros = [r[0] for r in conn.execute(
            "SELECT DISTINCT i.chamado FROM incidentes i LEFT JOIN chamados c ON c.numero = i.chamado "
            "WHERE i.chamado IS NOT NULL AND i.inicio >= ? "
            "AND (c.numero IS NULL OR c.encerrado = 0 OR c.atualizado_em < coalesce(c.encerrado_em, 0) + 3600)",
            (desde,))]
    lidos = acoes = erros = 0
    for n in numeros:
        try:
            inc = td.incidente(n)
            if not inc:
                print(f"[!] chamado {n} não encontrado no TOPdesk", file=sys.stderr)
                continue
            lista = td.acoes(n)
            gravar_chamado(conn, n, inc, lista)
            conn.commit()
            lidos += 1
            acoes += len(lista)
        except Exception as e:  # noqa: BLE001
            erros += 1
            print(f"[!] chamado {n}: {e}", file=sys.stderr)
    print(f"{len(numeros)} chamado(s) para consultar; {lidos} lidos; {acoes} ações; {erros} erro(s)")


def acao_principal(conn, numero):
    """Texto mostrado no relatório: a última ação registrada por analista (normalmente a tratativa/encerramento)."""
    r = conn.execute("SELECT texto FROM chamado_acoes WHERE chamado=? AND analista=1 AND texto <> '' "
                     "ORDER BY clock DESC LIMIT 1", (numero,)).fetchone()
    return r["texto"] if r else None


def limpar(conn, meses, agora=None, incluir_fechamentos=False):
    """Apaga dados mais antigos que o início do mês (atual − meses). Objetos e triggers ficam."""
    agora = int(agora or time.time())
    hoje = datetime.fromtimestamp(agora, TZ)
    ano, mes = hoje.year, hoje.month - meses
    while mes <= 0:
        mes += 12
        ano -= 1
    corte_mes = f"{ano:04d}-{mes:02d}"
    corte = limites_mes(corte_mes)[0]
    velhos = "SELECT id FROM incidentes WHERE coalesce(fim, inicio) < ? AND fim IS NOT NULL"
    n = {}
    n["comentarios"] = conn.execute(f"DELETE FROM comentarios WHERE incidente_id IN ({velhos})", (corte,)).rowcount
    conn.execute(f"UPDATE expurgos SET incidente_id = NULL WHERE incidente_id IN ({velhos}) AND fim >= ?",
                 (corte, corte))
    n["expurgos"] = conn.execute("DELETE FROM expurgos WHERE fim < ?", (corte,)).rowcount
    n["incidentes"] = conn.execute(velhos.replace("SELECT id", "DELETE"), (corte,)).rowcount
    n["lacunas"] = conn.execute("DELETE FROM lacunas WHERE fim < ?", (corte,)).rowcount
    n["textos_mensais"] = conn.execute("DELETE FROM textos_mensais WHERE mes < ?", (corte_mes,)).rowcount
    n["auditoria"] = conn.execute("DELETE FROM auditoria WHERE em < ?", (corte,)).rowcount
    n["chamado_acoes"] = conn.execute("DELETE FROM chamado_acoes WHERE chamado NOT IN "
                                      "(SELECT chamado FROM incidentes WHERE chamado IS NOT NULL)").rowcount
    n["chamados"] = conn.execute("DELETE FROM chamados WHERE numero NOT IN "
                                 "(SELECT chamado FROM incidentes WHERE chamado IS NOT NULL)").rowcount
    if incluir_fechamentos:
        ids = "SELECT id FROM fechamentos WHERE mes < ?"
        conn.execute(f"DELETE FROM fechamento_valores WHERE fechamento_id IN ({ids})", (corte_mes,))
        n["fechamentos"] = conn.execute("DELETE FROM fechamentos WHERE mes < ?", (corte_mes,)).rowcount
    conn.commit()
    return corte_mes, n


def contagem_item(conn, objeto_id):
    """O que uma remoção apagaria. None se o item não existe."""
    o = conn.execute("SELECT * FROM objetos WHERE id=?", (objeto_id,)).fetchone()
    if not o:
        return None
    um = lambda sql: conn.execute(sql, (objeto_id,)).fetchone()[0]
    return {"id": o["id"], "relatorio": o["relatorio"], "item": o["item"], "ativo": o["ativo_ate"] is None,
            "incidentes": um("SELECT count(*) FROM incidentes WHERE objeto_id=?"),
            "comentarios": um("SELECT count(*) FROM comentarios WHERE incidente_id IN (SELECT id FROM incidentes WHERE objeto_id=?)"),
            "expurgos": um("SELECT count(*) FROM expurgos WHERE objeto_id=?"),
            "chamados": um("SELECT count(DISTINCT chamado) FROM incidentes WHERE objeto_id=?"),
            "meses_fechados": [r[0] for r in conn.execute(
                "SELECT DISTINCT f.mes FROM fechamento_valores v JOIN fechamentos f ON f.id=v.fechamento_id "
                "WHERE v.objeto_id=? ORDER BY 1", (objeto_id,))]}


def remover_item(conn, objeto_id, por, incluir_fechados=False):
    """Apaga o item e tudo o que é só dele. Não mexe nos expurgos que valem para a medição inteira.
    Item ativo é recusado: com a tag ainda no Zabbix, ele voltaria na próxima sincronização."""
    c = contagem_item(conn, objeto_id)
    if not c:
        raise SystemExit("item não encontrado")
    if c["ativo"]:
        raise SystemExit(f"{c['item']} ainda está ativo: tire a tag da trigger no Zabbix e sincronize antes de remover")
    if c["meses_fechados"] and not incluir_fechados:
        raise SystemExit(f"{c['item']} faz parte do fechamento de {', '.join(c['meses_fechados'])}: confirme para incluir os valores congelados")
    chamados = [r[0] for r in conn.execute("SELECT DISTINCT chamado FROM incidentes WHERE objeto_id=? AND chamado IS NOT NULL", (objeto_id,))]
    dele = "SELECT id FROM incidentes WHERE objeto_id=?"
    try:
        conn.execute(f"DELETE FROM comentarios WHERE incidente_id IN ({dele})", (objeto_id,))
        conn.execute("DELETE FROM expurgos WHERE objeto_id=?", (objeto_id,))
        conn.execute(f"UPDATE expurgos SET incidente_id=NULL WHERE incidente_id IN ({dele})", (objeto_id,))
        conn.execute("DELETE FROM incidentes WHERE objeto_id=?", (objeto_id,))
        conn.execute("DELETE FROM objeto_triggers WHERE objeto_id=?", (objeto_id,))
        conn.execute("DELETE FROM fechamento_valores WHERE objeto_id=?", (objeto_id,))
        conn.execute("DELETE FROM objetos WHERE id=?", (objeto_id,))
        for n in chamados:                  # chamado que era só deste item não tem mais função
            if not conn.execute("SELECT 1 FROM incidentes WHERE chamado=? LIMIT 1", (n,)).fetchone():
                conn.execute("DELETE FROM chamado_acoes WHERE chamado=?", (n,))
                conn.execute("DELETE FROM chamados WHERE numero=?", (n,))
        conn.execute("INSERT INTO auditoria (tabela, registro, acao, antes, depois) VALUES ('objetos',?,'delete',?,?)",
                     (objeto_id, json.dumps(c, ensure_ascii=False), json.dumps({"por": por}, ensure_ascii=False)))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return c


# ------------------------------------------------------------ CLI ----

def main():
    ap = argparse.ArgumentParser(description="Banco de disponibilidade por eventos")
    ap.add_argument("--db", help="arquivo SQLite (padrão: DISP_DB ou ./disponibilidade.db)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    s = sub.add_parser("sync-objetos")
    s.add_argument("--desde", default=None, help="início da medição dos objetos NOVOS (padrão: agora)")
    s = sub.add_parser("importar-eventos")
    s.add_argument("--de", required=True)
    s.add_argument("--ate", required=True)
    s.add_argument("--margem-dias", type=int, default=7)
    s = sub.add_parser("calcular")
    s.add_argument("--relatorio", required=True)
    s.add_argument("--mes", required=True)
    s.add_argument("--grupo")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("incidentes", help="lista os incidentes de um relatório no mês")
    s.add_argument("--relatorio", required=True)
    s.add_argument("--mes", required=True)
    s.add_argument("--item", help="filtra por parte do nome do item (ex.: UHCC)")
    s.add_argument("--comentarios", action="store_true", help="mostra os comentários de cada incidente")
    s = sub.add_parser("expurgo-propor")
    s.add_argument("--objeto", type=int, help="id do objeto (ou --relatorio para todos)")
    s.add_argument("--relatorio")
    s.add_argument("--incidente", type=int)
    s.add_argument("--inicio", required=True)
    s.add_argument("--fim", required=True)
    s.add_argument("--tipo", required=True)
    s.add_argument("--motivo", required=True)
    s.add_argument("--chamado")
    s.add_argument("--por", required=True)
    s = sub.add_parser("expurgo-incidente", help="expurga uma queda pelo id do incidente (inteira ou um trecho)")
    s.add_argument("--incidente", type=int, required=True, help="id mostrado em 'incidentes' (#123)")
    s.add_argument("--inicio", help="só um trecho: início (padrão: início da queda)")
    s.add_argument("--fim", help="só um trecho: fim (padrão: fim da queda)")
    s.add_argument("--tipo", required=True,
                   help="falso_positivo | desligamento_nao_comunicado | manutencao_programada | energia_concessionaria | outro")
    s.add_argument("--motivo", required=True)
    s.add_argument("--por", required=True)
    s.add_argument("--aprovar", action="store_true", help="já aprova (fica registrado na auditoria)")
    s = sub.add_parser("desconsiderar", help="não contabilizar uma queda (fica no relatório, marcada) - sem aprovação")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--incidente", type=int, action="append", help="id da queda (veja em 'incidentes'); pode repetir")
    g.add_argument("--chamado", action="append", help="todas as quedas deste chamado; pode repetir")
    s.add_argument("--inicio", help="só um trecho: início AAAA-MM-DD HH:MM (padrão: a queda inteira)")
    s.add_argument("--fim", help="só um trecho: fim AAAA-MM-DD HH:MM")
    s.add_argument("--tipo", default="falso_positivo", choices=sorted(TIPOS_EXPURGO))
    s.add_argument("--motivo", required=True)
    s.add_argument("--por", required=True)
    s = sub.add_parser("contabilizar", help="volta a contabilizar: desfaz o 'desconsiderar' (fica na auditoria)")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--id", type=int, help="id do expurgo (veja em 'expurgos')")
    g.add_argument("--chamado", help="todos os expurgos deste chamado")
    s.add_argument("--por", required=True)
    s = sub.add_parser("expurgos", help="lista os expurgos")
    s.add_argument("--relatorio")
    s.add_argument("--mes")
    s.add_argument("--status", choices=["proposto", "aprovado", "rejeitado"])
    s = sub.add_parser("expurgo-revogar", help="desfaz um expurgo aprovado (vira rejeitado, com auditoria)")
    s.add_argument("--id", type=int, required=True)
    s.add_argument("--por", required=True)
    s = sub.add_parser("expurgo-decidir")
    s.add_argument("--id", type=int, required=True)
    s.add_argument("--status", choices=["aprovado", "rejeitado"], required=True)
    s.add_argument("--por", required=True)
    s = sub.add_parser("limpar", help="apaga dados anteriores a (mês atual − N meses)")
    s.add_argument("--meses", type=int, default=3, help="meses fechados a manter além do atual (padrão 3)")
    s.add_argument("--incluir-fechamentos", action="store_true", help="apaga também os fechamentos antigos")
    s = sub.add_parser("importar-chamados", help="busca no TOPdesk os chamados dos incidentes e as ações")
    s.add_argument("--dias", type=int, default=45, help="incidentes iniciados nos últimos N dias (padrão 45)")
    s.add_argument("--chamado", action="append", help="só este(s) número(s)")
    s = sub.add_parser("chamado", help="mostra um chamado e as ações gravadas")
    s.add_argument("numero")
    s = sub.add_parser("remover-item", help="apaga de vez um item encerrado e tudo o que é só dele")
    s.add_argument("--id", type=int, required=True, help="id do item (objeto)")
    s.add_argument("--por", required=True)
    s.add_argument("--incluir-fechados", action="store_true", help="apaga também os valores congelados de meses fechados")
    s = sub.add_parser("lacuna")
    s.add_argument("--relatorio")
    s.add_argument("--inicio", required=True)
    s.add_argument("--fim", required=True)
    s.add_argument("--motivo", required=True)
    s.add_argument("--por", required=True)
    a = ap.parse_args()

    conn = conectar(a.db)
    if a.cmd == "init":
        print("Banco pronto:", conn.execute("PRAGMA database_list").fetchone()["file"])
    elif a.cmd == "sync-objetos":
        sync_objetos(conn, epoch(a.desde) if a.desde else int(time.time()))
    elif a.cmd == "importar-eventos":
        importar_eventos(conn, a.de, a.ate, a.margem_dias)
    elif a.cmd == "calcular":
        linhas = calcular(conn, a.relatorio, a.mes, grupo=a.grupo)
        if a.json:
            print(json.dumps({"relatorio": a.relatorio, "mes": a.mes, "objetos": linhas,
                              "grupos": agregar(linhas)}, ensure_ascii=False, indent=2))
        else:
            for l in sorted(linhas, key=lambda x: (x["disponibilidade"] is None, x["disponibilidade"] or 0)):
                print(f"{pct(l['disponibilidade']):>8}  {l['grupo'] or '-':<10} {l['nome']:<40} "
                      f"fora {dur(l['segundos_fora'])}  expurgo {dur(l['segundos_expurgo'])}  "
                      f"incidentes {l['incidentes']}")
            for g, v in agregar(linhas).items():
                print(f"Média {g or '(sem grupo)'}: {pct(v)}")
    elif a.cmd == "incidentes":
        m_ini, m_fim = limites_mes(a.mes)
        sql = ("SELECT i.id, o.id AS objeto, o.item, i.inicio, i.fim, i.problema, i.chamado FROM incidentes i "
               "JOIN objetos o ON o.id = i.objeto_id WHERE o.relatorio = ? AND i.inicio < ? "
               "AND coalesce(i.fim, ?) > ?")
        args = [a.relatorio, m_fim, int(time.time()), m_ini]
        if a.item:
            sql += " AND o.item LIKE ?"
            args.append(f"%{a.item}%")
        fmt = lambda t: datetime.fromtimestamp(t, TZ).strftime("%d/%m %H:%M:%S") if t else "aberto"
        for r in conn.execute(sql + " ORDER BY i.inicio", args):
            d = ((r["fim"] or time.time()) - r["inicio"])
            print(f"#{r['id']:<6} obj {r['objeto']:<4} {r['item']:<28} {fmt(r['inicio'])} → {fmt(r['fim'])}  "
                  f"{int(d // 60):>4} min  {r['problema'] or ''}{'  [' + r['chamado'] + ']' if r['chamado'] else ''}"
                  + {"total": "  <EXPURGADO>", "parcial": "  <expurgo parcial>"}.get(situacao_expurgo(conn, r["id"])[0], ""))
            if a.comentarios:
                for c in conn.execute("SELECT clock, usuario, mensagem FROM comentarios WHERE incidente_id=? "
                                      "ORDER BY clock", (r["id"],)):
                    print(f"         {fmt(c['clock'])}  {c['usuario'] or '-'}: {c['mensagem']}")
    elif a.cmd == "expurgo-propor":
        cur = conn.execute(
            "INSERT INTO expurgos (objeto_id, relatorio, incidente_id, inicio, fim, tipo, motivo, chamado, proposto_por) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (a.objeto, a.relatorio, a.incidente, epoch(a.inicio), epoch(a.fim), a.tipo, a.motivo, a.chamado, a.por))
        conn.commit()
        print(json.dumps({"expurgo_id": cur.lastrowid, "status": "proposto"}))
    elif a.cmd == "expurgo-incidente":
        i = conn.execute("SELECT i.*, o.item, o.relatorio FROM incidentes i JOIN objetos o ON o.id = i.objeto_id "
                         "WHERE i.id = ?", (a.incidente,)).fetchone()
        if not i:
            raise SystemExit("incidente não encontrado (veja o id com: disp.py incidentes ...)")
        ini = epoch(a.inicio) if a.inicio else i["inicio"]
        fim = epoch(a.fim) if a.fim else (i["fim"] or int(time.time()))
        cur = conn.execute(
            "INSERT INTO expurgos (objeto_id, incidente_id, inicio, fim, tipo, motivo, chamado, proposto_por) "
            "VALUES (?,?,?,?,?,?,?,?)", (i["objeto_id"], i["id"], ini, fim, a.tipo, a.motivo, i["chamado"], a.por))
        eid = cur.lastrowid
        if a.aprovar:
            conn.execute("UPDATE expurgos SET status='aprovado', decidido_por=?, decidido_em=strftime('%s','now') "
                         "WHERE id=?", (a.por, eid))
        conn.commit()
        fmt = lambda t: datetime.fromtimestamp(t, TZ).strftime("%d/%m/%Y %H:%M:%S")
        print(f"expurgo #{eid} ({'aprovado' if a.aprovar else 'proposto'}): {i['relatorio']} / {i['item']} "
              f"{fmt(ini)} → {fmt(fim)}  [{a.tipo}] {a.motivo}")
        if not a.aprovar:
            print(f"para aprovar: disp.py expurgo-decidir --id {eid} --status aprovado --por <nome>")
    elif a.cmd == "desconsiderar":
        ids = a.incidente or [r[0] for n in a.chamado for r in conn.execute(
            "SELECT id FROM incidentes WHERE chamado = ? ORDER BY inicio", (n,))]
        if not ids:
            raise SystemExit("nenhuma queda encontrada para esse chamado")
        fmt = lambda t: datetime.fromtimestamp(t, TZ).strftime("%d/%m/%Y %H:%M:%S")
        criados = desconsiderar(conn, ids, a.tipo, a.motivo, a.por,
                                epoch(a.inicio) if a.inicio else None, epoch(a.fim) if a.fim else None)
        for eid, rel, item, ini, fim in criados:
            print(f"expurgo #{eid}: {rel} / {item}  {fmt(ini)} → {fmt(fim)}  [{TIPOS_EXPURGO[a.tipo]}] {a.motivo}")
        print(f"{len(criados)} queda(s) desconsiderada(s)" + ("" if criados else " (já estavam desconsideradas)"))
    elif a.cmd == "contabilizar":
        if a.id:
            cur = conn.execute("UPDATE expurgos SET status='rejeitado', decidido_por=?, decidido_em=strftime('%s','now') "
                               "WHERE id=? AND status='aprovado'", (a.por, a.id))
        else:
            cur = conn.execute("UPDATE expurgos SET status='rejeitado', decidido_por=?, decidido_em=strftime('%s','now') "
                               "WHERE status='aprovado' AND (chamado = ? OR incidente_id IN "
                               "(SELECT id FROM incidentes WHERE chamado = ?))", (a.por, a.chamado, a.chamado))
        conn.commit()
        print(f"{cur.rowcount} expurgo(s) desfeito(s): a(s) queda(s) volta(m) a contar")
    elif a.cmd == "expurgos":
        sql = ("SELECT e.*, coalesce(o.relatorio, e.relatorio) AS rel, coalesce(o.item, '(relatório inteiro)') AS item "
               "FROM expurgos e LEFT JOIN objetos o ON o.id = e.objeto_id WHERE 1=1")
        args = []
        if a.relatorio:
            sql += " AND coalesce(o.relatorio, e.relatorio) = ?"
            args.append(a.relatorio)
        if a.mes:
            m_ini, m_fim = limites_mes(a.mes)
            sql += " AND e.inicio < ? AND e.fim > ?"
            args += [m_fim, m_ini]
        if a.status:
            sql += " AND e.status = ?"
            args.append(a.status)
        fmt = lambda t: datetime.fromtimestamp(t, TZ).strftime("%d/%m %H:%M") if t else "-"
        for e in conn.execute(sql + " ORDER BY e.inicio", args):
            print(f"#{e['id']:<5} {e['status']:<9} {e['rel']:<15} {e['item'][:40]:<40} {fmt(e['inicio'])} → {fmt(e['fim'])}  "
                  f"[{e['tipo']}] {e['motivo']}  (por {e['proposto_por']}"
                  f"{', decidido por ' + e['decidido_por'] if e['decidido_por'] else ''})")
    elif a.cmd == "expurgo-revogar":
        cur = conn.execute("UPDATE expurgos SET status='rejeitado', decidido_por=?, decidido_em=strftime('%s','now') "
                           "WHERE id=? AND status='aprovado'", (a.por, a.id))
        conn.commit()
        if not cur.rowcount:
            raise SystemExit("expurgo não encontrado ou não está aprovado")
        print(json.dumps({"expurgo_id": a.id, "status": "rejeitado"}))
    elif a.cmd == "expurgo-decidir":
        cur = conn.execute("UPDATE expurgos SET status=?, decidido_por=?, decidido_em=strftime('%s','now') "
                           "WHERE id=? AND status='proposto'", (a.status, a.por, a.id))
        conn.commit()
        if not cur.rowcount:
            raise SystemExit("expurgo não encontrado ou já decidido")
        print(json.dumps({"expurgo_id": a.id, "status": a.status}))
    elif a.cmd == "limpar":
        corte, n = limpar(conn, a.meses, incluir_fechamentos=a.incluir_fechamentos)
        print(f"mantido a partir de {corte}; apagados: " + ", ".join(f"{k} {v}" for k, v in n.items()))
    elif a.cmd == "importar-chamados":
        importar_chamados(conn, a.dias, a.chamado)
    elif a.cmd == "chamado":
        c = conn.execute("SELECT * FROM chamados WHERE numero=?", (a.numero,)).fetchone()
        if not c:
            raise SystemExit("chamado não importado (rode importar-chamados)")
        fmt = lambda t: datetime.fromtimestamp(t, TZ).strftime("%d/%m/%Y %H:%M") if t else "-"
        print(f"{c['numero']}  {c['titulo'] or ''}\n  status: {c['status']}  aberto: {fmt(c['aberto_em'])}  "
              f"encerrado: {fmt(c['encerrado_em']) if c['encerrado'] else 'não'}")
        for x in conn.execute("SELECT * FROM chamado_acoes WHERE chamado=? ORDER BY clock", (a.numero,)):
            print(f"\n  {fmt(x['clock'])}  {x['autor'] or '-'}{'' if x['analista'] else ' (solicitante/sistema)'}")
            print("    " + (x["texto"] or "").replace("\n", "\n    "))
        print(f"\n  ação no relatório: {acao_principal(conn, a.numero) or '-'}")
    elif a.cmd == "remover-item":
        c = remover_item(conn, a.id, a.por, a.incluir_fechados)
        print(f"{c['relatorio']} / {c['item']} removido: {c['incidentes']} queda(s), {c['expurgos']} expurgo(s)")
    elif a.cmd == "lacuna":
        conn.execute("INSERT INTO lacunas (relatorio, inicio, fim, motivo, criado_por) VALUES (?,?,?,?,?)",
                     (a.relatorio, epoch(a.inicio), epoch(a.fim), a.motivo, a.por))
        conn.commit()
        print("lacuna registrada")


if __name__ == "__main__":
    main()
