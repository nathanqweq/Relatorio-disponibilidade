"""Testes do painel: sobem o admin.py de verdade numa cópia temporária da pasta e falam HTTP com ele.
Rodar: python3 -m pytest -q testes/test_admin.py"""
import http.cookiejar
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ORIGEM = Path(__file__).resolve().parent.parent
SENHA = "senha-de-teste-123"
SEGREDO = "TOKEN-SECRETO-XYZ"
MES_PASSADO = "2026-07"


class Cliente:
    def __init__(self, base):
        self.base = base
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def pedir(self, caminho, corpo=None, headers=None):
        h = {"Content-Type": "application/json", "X-Painel": "1"} if corpo is not None else {}
        h.update(headers or {})
        dados = json.dumps(corpo).encode() if corpo is not None else None
        req = urllib.request.Request(self.base + caminho, data=dados, headers=h)
        try:
            with self.op.open(req, timeout=60) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def get(self, caminho):
        s, b, _ = self.pedir(caminho)
        return s, json.loads(b)

    def post(self, caminho, corpo=None):
        s, b, _ = self.pedir(caminho, corpo if corpo is not None else {})
        return s, json.loads(b)

    def entrar(self, usuario="ana", senha=SENHA):
        return self.post("/api/login", {"usuario": usuario, "senha": senha})

    def tarefa(self, tipo, **parametros):
        s, t = self.post("/api/tarefas", {"tipo": tipo, "parametros": parametros})
        assert s == 200, t
        for _ in range(300):
            s, d = self.get(f"/api/tarefas/{t['id']}")
            if d["estado"] != "executando":
                return d
            time.sleep(0.1)
        raise AssertionError("tarefa não terminou")


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    pasta = tmp_path_factory.mktemp("painel")
    for nome in ("admin.py", "disp.py", "api.py", "relatorio.py", "schema.sql", "relatorios.json"):
        shutil.copy(ORIGEM / nome, pasta / nome)
    shutil.copytree(ORIGEM / "admin_web", pasta / "admin_web")
    (pasta / "testes").mkdir()
    for nome in ("conftest.py", "test_disp.py"):
        shutil.copy(ORIGEM / "testes" / nome, pasta / "testes" / nome)
    # PDF sem depender do WeasyPrint: o teste é do fluxo, não do desenho
    stub = tmp_path_factory.mktemp("stub")
    (stub / "weasyprint.py").write_text(
        "class HTML:\n    def __init__(self, string=None): pass\n    def write_pdf(self): return b'%PDF-teste'\n",
        encoding="utf-8")
    db = pasta / "disponibilidade.db"
    (pasta / "env").write_text(f"# comentário preservado\nDISP_DB={db}\nZABBIX_TOKEN={SEGREDO}\nOUTRA=fica\n", encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(stub), PYTHONIOENCODING="utf-8", DISP_DB=str(db))
    subprocess.run([sys.executable, "disp.py", "init"], cwd=pasta, env=env, check=True, capture_output=True)
    c = sqlite3.connect(db)
    c.execute("INSERT INTO objetos (id, relatorio, item, grupo, nome, ativo_desde) VALUES "
              "(1,'tid-usinas','GA_A','GERACAO','GA_A',0),(2,'tid-usinas','NE_B','{$LINK.OPERADORA}','NE_B',0)")
    c.execute("INSERT INTO incidentes (id, objeto_id, zabbix_eventid, inicio, fim, problema, chamado) VALUES "
              "(10,1,'e1',1784000000,1784003600,'<img src=x onerror=alert(1)>','I2607-1'),"
              "(11,2,'e2',1784100000,1784103600,'queda 2',NULL)")
    c.commit()
    c.close()
    sem = subprocess.run([sys.executable, "admin.py", "servidor"], cwd=pasta, env=env, capture_output=True, text=True)
    assert sem.returncode != 0 and "usuario" in sem.stderr       # sem usuário o painel não sobe
    r = subprocess.run([sys.executable, "admin.py", "usuario", "ana"], cwd=pasta, env=env, input=SENHA + "\n",
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        porta = s.getsockname()[1]
    p = subprocess.Popen([sys.executable, "admin.py", "servidor"], cwd=pasta,
                         env=dict(env, ADMIN_HOST="127.0.0.1", ADMIN_PORT=str(porta)),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{porta}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/", timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    yield base, pasta
    p.terminate()
    p.wait(10)


@pytest.fixture
def anon(srv):
    return Cliente(srv[0])


@pytest.fixture
def cli(srv):
    c = Cliente(srv[0])
    assert c.entrar()[0] == 200
    return c


def test_pagina_abre_com_cabecalhos_de_seguranca(anon):
    s, corpo, h = anon.pedir("/")
    assert s == 200 and b"app.js" in corpo
    assert "default-src 'self'" in h["Content-Security-Policy"] and h["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize("rota", ["/api/painel", "/api/config", "/api/quedas?relatorio=x", "/api/pdfs", "/api/backups",
                                  "/api/usuarios", "/api/tarefas", "/api/auditoria"])
def test_rotas_exigem_login(anon, rota):
    assert anon.get(rota)[0] == 401


def test_login_errado_e_bloqueio(anon):
    for _ in range(5):
        assert anon.entrar("bruto", "errada")[0] == 401
    assert anon.entrar("bruto", "errada")[0] == 429
    assert anon.entrar()[0] == 200                  # outro usuário não é afetado


def test_cookie_protegido(anon):
    s, _, h = anon.pedir("/api/login", {"usuario": "ana", "senha": SENHA})
    assert s == 200
    assert "HttpOnly" in h["Set-Cookie"] and "SameSite=Strict" in h["Set-Cookie"]


def test_post_de_outro_site_recusado(cli):
    assert cli.pedir("/api/backups", {}, {"X-Painel": ""})[0] == 403
    assert cli.pedir("/api/backups", {}, {"Origin": "http://atacante.exemplo"})[0] == 403
    assert cli.pedir("/api/backups", {}, {"Content-Type": "text/plain"})[0] == 403


def test_logout_encerra_a_sessao(srv):
    c = Cliente(srv[0])
    c.entrar()
    assert c.post("/api/logout")[0] == 200
    assert c.get("/api/painel")[0] == 401


def test_painel_aponta_macro_sem_valor(cli):
    s, d = cli.get("/api/painel")
    assert s == 200
    assert any("macro" in a["texto"] for a in d["alertas"])
    assert {m["relatorio"]: m["itens"] for m in d["medicoes"]} == {"tid-usinas": 2}


def test_desconsiderar_e_voltar_a_contar(cli):
    s, d = cli.post("/api/quedas/desconsiderar", {"incidentes": [10], "tipo": "falso_positivo", "motivo": "-- alarme falso"})
    assert s == 200, d
    s, linhas = cli.get(f"/api/quedas?relatorio=tid-usinas&mes={MES_PASSADO}")
    q = {l["ID"]: l for l in linhas}
    assert q[10]["EXPURGO"] == "total" and "alarme falso (por ana)" in q[10]["MOTIVO_EXPURGO"]
    assert q[11]["EXPURGO"] is None
    s, det = cli.get("/api/quedas/10")
    assert det["expurgos"][0]["proposto_por"] == "ana"
    assert det["problema"] == "<img src=x onerror=alert(1)>"   # guardado como texto; a tela nunca usa innerHTML
    s, d = cli.post("/api/quedas/contabilizar", {"incidentes": [10]})
    assert s == 200, d
    s, linhas = cli.get(f"/api/quedas?relatorio=tid-usinas&mes={MES_PASSADO}")
    assert {l["ID"]: l for l in linhas}[10]["EXPURGO"] is None


def test_quedas_validacoes(cli):
    assert cli.post("/api/quedas/desconsiderar", {"incidentes": [10], "tipo": "inventado", "motivo": "x"})[0] == 400
    assert cli.post("/api/quedas/desconsiderar", {"incidentes": [10], "tipo": "outro", "motivo": "  "})[0] == 400
    assert cli.post("/api/quedas/desconsiderar", {"incidentes": ["1; rm"], "tipo": "outro", "motivo": "x"})[0] == 400
    assert cli.post("/api/quedas/desconsiderar", {"incidentes": [99999], "tipo": "outro", "motivo": "x"})[0] == 400
    assert cli.get("/api/quedas?relatorio=tid-usinas&mes=abc")[0] == 400
    assert cli.get("/api/quedas?relatorio=tid-usinas&de=2026-07-10&ate=2026-07-01")[0] == 400


def test_expurgo_de_janela_fluxo_completo(cli):
    corpo = {"relatorio": "tid-usinas", "inicio": "2026-07-14T00:00", "fim": "2026-07-14T01:00", "tipo": "manutencao_programada", "motivo": "janela"}
    assert cli.post("/api/expurgos", dict(corpo, fim="2026-07-13T00:00"))[0] == 400
    assert cli.post("/api/expurgos", dict(corpo, relatorio="nao-existe"))[0] == 400
    assert cli.post("/api/expurgos", dict(corpo, objeto=999))[0] == 400
    s, d = cli.post("/api/expurgos", corpo)
    assert s == 200, d
    s, lista = cli.get(f"/api/expurgos?mes={MES_PASSADO}&status=proposto")
    eid = lista[0]["id"]
    assert lista[0]["item"] == "(medição inteira)"
    assert cli.post(f"/api/expurgos/{eid}/aprovar")[0] == 200
    assert cli.post(f"/api/expurgos/{eid}/aprovar")[0] == 400      # já decidido
    assert cli.post(f"/api/expurgos/{eid}/revogar")[0] == 200
    assert cli.get(f"/api/expurgos?status=rejeitado")[1][0]["decidido_por"] == "ana"


def test_lacuna_criar_e_remover(cli):
    s, d = cli.post("/api/lacunas", {"inicio": "2026-07-20T10:00", "fim": "2026-07-20T11:00", "motivo": "zabbix fora"})
    assert s == 200, d
    s, lista = cli.get("/api/lacunas")
    assert lista[0]["minutos"] == 60 and lista[0]["criado_por"] == "ana"
    assert cli.post(f"/api/lacunas/{lista[0]['id']}/remover")[0] == 200
    assert cli.get("/api/lacunas")[1] == []
    auditoria = cli.get("/api/auditoria")[1]
    assert any(a["tabela"] == "lacunas" for a in auditoria["banco"])


def test_itens_filtra_e_marca_macro(cli):
    s, itens = cli.get(f"/api/itens?relatorio=tid-usinas&mes={MES_PASSADO}")
    assert s == 200
    por = {i["item"]: i for i in itens}
    assert por["NE_B"]["macro_pendente"] and not por["GA_A"]["macro_pendente"]
    assert por["GA_A"]["disponibilidade"] < 100


def test_config_nao_expoe_segredo(cli, srv):
    s, corpo, _ = cli.pedir("/api/config")
    assert s == 200 and SEGREDO.encode() not in corpo
    z = {c["chave"]: c for c in json.loads(corpo)["campos"]}["ZABBIX_TOKEN"]
    assert z["definido"] and z["valor"] is None
    assert SEGREDO not in (srv[1] / "admin_auditoria.log").read_text(encoding="utf-8")


def test_config_grava_preservando_o_resto(cli, srv):
    s, d = cli.post("/api/config", {"valores": {"ZABBIX_URL": "https://z.exemplo", "CHAMADO_REGEX": r"(I\d+-\d+) $x"}, "remover": []})
    assert s == 200, d
    env = (srv[1] / "env").read_text(encoding="utf-8")
    assert "# comentário preservado" in env and "OUTRA=fica" in env and f"ZABBIX_TOKEN={SEGREDO}" in env
    assert "ZABBIX_URL=https://z.exemplo" in env and r"CHAMADO_REGEX='(I\d+-\d+) $x'" in env
    s, d = cli.post("/api/config", {"valores": {}, "remover": ["CHAMADO_REGEX"]})
    assert "CHAMADO_REGEX" not in (srv[1] / "env").read_text(encoding="utf-8")


@pytest.mark.parametrize("valores", [{"LD_PRELOAD": "/tmp/x.so"}, {"PATH": "/tmp"}, {"ZABBIX_URL": "ftp://x"},
                                     {"ZABBIX_TOKEN": "a\nLD_PRELOAD=/x"}, {"ZABBIX_TOKEN": "a'b"},
                                     {"ZABBIX_VERIFY_TLS": "talvez"}, {"CHAMADO_REGEX": "sem grupo"}, {"CHAMADO_REGEX": "("}])
def test_config_recusa_valores_perigosos(cli, srv, valores):
    antes = (srv[1] / "env").read_text(encoding="utf-8")
    assert cli.post("/api/config", {"valores": valores})[0] == 400
    assert (srv[1] / "env").read_text(encoding="utf-8") == antes


def definicao_ok():
    return {"titulo": "T", "arquivo": "T {mes_nome} {ano}", "meta": 99.9,
            "secoes": [{"tipo": "tabela", "titulo": "S", "relatorio": "tid-usinas"}]}


@pytest.mark.parametrize("mudar,pedaco", [
    (lambda d: d.pop("arquivo"), "arquivo"),
    (lambda d: d.update(arquivo="{relatorio}"), "arquivo"),
    (lambda d: d.update(arquivo="../../etc/x"), "barras"),
    (lambda d: d.update(arquivo="{"), "malformadas"),
    (lambda d: d.update(meta="alta"), "meta"),
    (lambda d: d.update(secoes=[]), "secoes"),
    (lambda d: d["secoes"][0].update(tipo="grafico"), "tipo"),
    (lambda d: d["secoes"][0].pop("relatorio"), "relatorio"),
    (lambda d: d["secoes"][0].update(tipo="indicadores", indicadores=[{"rotulo": "x", "chave": "Y"}]), "sem relatorio"),
])
def test_definicao_invalida_nao_grava(cli, srv, mudar, pedaco):
    antes = (srv[1] / "relatorios.json").read_bytes()
    d = definicao_ok()
    mudar(d)
    s, r = cli.post("/api/definicoes/novo-rel", {"definicao": d, "novo": True})
    assert s == 400 and pedaco in r["erro"]
    assert (srv[1] / "relatorios.json").read_bytes() == antes


def test_definicoes_atuais_passam_na_validacao(cli):
    defs = cli.get("/api/definicoes")[1]["definicoes"]
    assert len(defs) == 8
    for rid, d in defs.items():
        s, r = cli.post(f"/api/definicoes/{rid}", {"definicao": d})
        assert s == 200, (rid, r)


def test_definicao_criar_e_remover(cli, srv):
    s, r = cli.post("/api/definicoes/teste-novo", {"definicao": definicao_ok(), "novo": True})
    assert s == 200 and r["aviso"] is None, r
    assert cli.post("/api/definicoes/teste-novo", {"definicao": definicao_ok(), "novo": True})[0] == 409
    assert cli.post("/api/definicoes/inexistente", {"definicao": definicao_ok()})[0] == 404
    d = definicao_ok()
    d["secoes"][0]["relatorio"] = "sem-itens"
    assert "sem-itens" in cli.post("/api/definicoes/teste-novo", {"definicao": d})[1]["aviso"]
    assert cli.post("/api/definicoes/teste-novo/remover")[0] == 200
    assert "teste-novo" not in json.loads((srv[1] / "relatorios.json").read_text(encoding="utf-8"))
    assert list((srv[1] / "backups").glob("relatorios-*.json"))


def test_tarefa_desconhecida_e_parametros_invalidos(cli):
    assert cli.post("/api/tarefas", {"tipo": "rm -rf /"})[0] == 400
    assert cli.post("/api/tarefas", {"tipo": "limpar", "parametros": {"meses": 0}})[0] == 400
    assert cli.post("/api/tarefas", {"tipo": "fechar", "parametros": {"relatorio": "--todos", "mes": MES_PASSADO}})[0] == 400
    assert cli.post("/api/tarefas", {"tipo": "importar-eventos", "parametros": {"de": "; ls", "ate": "2026-07-01"}})[0] == 400
    assert cli.post("/api/tarefas", {"tipo": "importar-chamados", "parametros": {"chamado": "--x"}})[0] == 400
    assert cli.post("/api/tarefas", {"tipo": "carga-inicial", "parametros": {"desde": "--x"}})[0] == 400


def test_tarefa_testes_do_calculo_acha_os_testes(cli):
    assert cli.tarefa("testes")["estado"] == "concluida"


def test_sem_n8n_no_painel(cli):
    assert b"n8n" not in cli.pedir("/api/listas")[1].lower()
    assert b"n8n" not in cli.pedir("/api/painel")[1].lower()
    assert b"n8n" not in cli.pedir("/api/config")[1].lower()
    assert b"n8n" not in cli.pedir("/app.js")[1].lower()
    assert cli.post("/api/tarefas", {"tipo": "email", "parametros": {"relatorio": "tid", "mes": MES_PASSADO}})[0] == 400


def test_tarefa_falha_mostra_o_erro(cli, srv):
    d = cli.tarefa("testar-topdesk")                      # sem TOPDESK_URL no env
    assert d["estado"] == "falhou" and d["codigo"] != 0
    s, d2 = cli.get(f"/api/tarefas/{d['id']}?desde=0")
    assert any("TOPDESK_URL" in l for l in d2["saida"])


def test_fechamento_pdf_e_nova_versao(cli):
    assert cli.tarefa("fechar", relatorio="firewalls-sci", mes=MES_PASSADO)["estado"] == "concluida"
    s, d = cli.get(f"/api/fechamentos?mes={MES_PASSADO}")
    fw = {r["id"]: r for r in d["relatorios"]}["firewalls-sci"]
    assert d["terminou"] and fw["versoes"][0]["versao"] == 1 and fw["versoes"][0]["gerado_por"] == "ana"
    s, corpo, h = cli.pedir("/api/pdf?caminho=" + urllib.request.quote(fw["versoes"][0]["pdf"]))
    assert s == 200 and corpo == b"%PDF-teste" and "attachment" in h["Content-Disposition"]
    assert cli.tarefa("fechar", relatorio="firewalls-sci", mes=MES_PASSADO, nova_versao=True)["estado"] == "concluida"
    fw = {r["id"]: r for r in cli.get(f"/api/fechamentos?mes={MES_PASSADO}")[1]["relatorios"]}["firewalls-sci"]
    assert [v["versao"] for v in fw["versoes"]] == [2, 1]
    tipos = {p["tipo"] for p in cli.get("/api/pdfs")[1]}
    assert "oficial" in tipos
    oficial = next(p for p in cli.get("/api/pdfs")[1] if p["tipo"] == "oficial")
    assert cli.post("/api/pdfs/remover", {"caminho": oficial["caminho"]})[0] == 403


def test_aviso_de_mes_fechado(cli):
    cli.tarefa("fechar", relatorio="tid", mes=MES_PASSADO)
    s, d = cli.post("/api/quedas/desconsiderar", {"incidentes": [11], "tipo": "outro", "motivo": "x"})
    assert s == 200 and MES_PASSADO in d["aviso"]
    cli.post("/api/quedas/contabilizar", {"incidentes": [11]})


def test_gerar_avulso_e_remover(cli):
    d = cli.tarefa("gerar", relatorio="firewalls-sci", de="2026-07-01", ate="2026-07-05")
    assert d["estado"] == "concluida", cli.get(f"/api/tarefas/{d['id']}?desde=0")[1]["saida"]
    assert cli.tarefa("gerar", mes=MES_PASSADO)["estado"] == "concluida"
    avulsos = [p for p in cli.get("/api/pdfs")[1] if p["tipo"] == "avulso"]
    assert len(avulsos) == 8 + 1
    assert cli.post("/api/pdfs/remover", {"caminho": avulsos[0]["caminho"]})[0] == 200


@pytest.mark.parametrize("caminho", ["../env", "../../etc/passwd", "../relatorios.json", "..%2Fenv", "/etc/passwd", ""])
def test_download_nao_sai_da_pasta(cli, caminho):
    assert cli.pedir("/api/pdf?caminho=" + caminho)[0] in (400, 404)


@pytest.mark.parametrize("nome", ["../env", "..%2Fenv", "env", "admin_usuarios.json", "../admin_usuarios.json"])
def test_backup_nao_baixa_outros_arquivos(cli, nome):
    assert cli.pedir("/api/backup?nome=" + nome)[0] in (400, 404)


def test_backup_consistente(cli, srv):
    assert cli.post("/api/backups")[0] == 200
    b = [x for x in cli.get("/api/backups")[1] if x["tipo"] == "banco"][0]
    s, corpo, _ = cli.pedir("/api/backup?nome=" + b["nome"])
    assert s == 200 and corpo.startswith(b"SQLite format 3")
    c = sqlite3.connect(srv[1] / "backups" / b["nome"])
    assert c.execute("SELECT count(*) FROM objetos").fetchone()[0] == 2
    c.close()
    assert cli.post("/api/backups/remover", {"nome": b["nome"]})[0] == 200


def test_usuarios(srv, cli):
    assert cli.post("/api/usuarios", {"nome": "bia", "senha": "curta"})[0] == 400
    assert cli.post("/api/usuarios", {"nome": "../x", "senha": SENHA})[0] == 400
    assert cli.post("/api/usuarios", {"nome": "bia", "senha": SENHA})[0] == 200
    assert cli.post("/api/usuarios", {"nome": "bia", "senha": SENHA})[0] == 409
    bia = Cliente(srv[0])
    assert bia.entrar("bia")[0] == 200
    corpo = cli.pedir("/api/usuarios")[1]
    assert b"pbkdf2" not in corpo and b"hash" not in corpo
    # trocar a senha da bia derruba as sessões dela
    assert cli.post("/api/usuarios/bia/senha", {"senha": "outra-senha-456"})[0] == 200
    assert bia.get("/api/painel")[0] == 401
    assert bia.entrar("bia", "outra-senha-456")[0] == 200
    # a própria senha exige a atual
    assert bia.post("/api/usuarios/bia/senha", {"atual": "errada", "senha": "mais-uma-senha-789"})[0] == 403
    assert bia.post("/api/usuarios/bia/remover")[0] == 400       # não se remove
    assert cli.post("/api/usuarios/bia/remover")[0] == 200
    assert bia.get("/api/painel")[0] == 401                      # acesso cortado na hora


def test_servicos_so_na_lista_permitida(cli):
    assert cli.post("/api/servicos/ssh.service/stop")[0] == 403
    assert cli.post("/api/servicos/relatorios-engie-admin.service/stop")[0] == 403
    assert cli.post("/api/servicos/relatorios-engie-atualizar.service/stop")[0] == 403
    assert cli.get("/api/logs?unidade=ssh.service")[0] == 400


def test_auditoria_registra_quem_fez(cli):
    cli.post("/api/backups")
    painel = cli.get("/api/auditoria")[1]["painel"]
    assert painel[0]["usuario"] == "ana" and painel[0]["acao"] == "backup-criar"
    assert any(a["acao"] == "login-falhou" for a in painel)
