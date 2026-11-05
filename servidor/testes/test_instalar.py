"""Testes do instalador: roda o instalar.py de verdade numa pasta temporária, com apt-get e systemctl
falsos e um Zabbix falso respondendo por HTTP.  Rodar: python3 -m pytest -q testes/test_instalar.py"""
import hashlib
import json
import os
import re
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

PACOTE = Path(__file__).resolve().parent.parent.parent
TOKEN = "token-de-teste-$x"
SENHA = "senha-de-teste-123"


class ZabbixFalso(BaseHTTPRequestHandler):
    def do_POST(self):
        d = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        p, m = d["params"], d["method"]
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            r = {"error": {"message": "token inválido", "data": ""}}
        elif m == "trigger.get":
            r = {"result": [{"triggerid": "1", "description": "Down", "status": "0",
                             "tags": [{"tag": "relatorio", "value": "tid-usinas"}, {"tag": "item", "value": "{HOST.NAME}"}],
                             "hosts": [{"hostid": "9", "host": "GA", "name": "GA_UHSA"}]}]}
        elif m == "host.get":
            r = {"result": [{"hostid": "9", "description": "", "tags": [], "macros": [], "parentTemplates": []}]}
        elif m == "event.get" and "objectids" in p:
            r = {"result": [{"eventid": "100", "clock": str(p["time_from"] + 3600), "r_eventid": "0", "severity": "4",
                             "name": "Down", "objectid": "1", "acknowledges": []}]}
        else:
            r = {"result": []}
        b = json.dumps({"jsonrpc": "2.0", "id": 1, **r}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def zabbix():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ZabbixFalso)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def bin_falso(tmp_path):
    """Comandos do sistema que só anotam a chamada."""
    pasta = tmp_path / "bin"
    pasta.mkdir()
    log = pasta / "chamadas.txt"
    log.write_text("")
    apt, parado = pasta / "apt_rodou", pasta / "parado"     # dpkg-query só acha os pacotes depois do apt-get install
    for nome in ("apt-get", "systemctl", "hostname", "dpkg-query"):
        if os.name == "nt":
            corpo = f'@echo off\r\necho {nome} %*>> "{log}"\r\n'
            if nome == "apt-get":
                corpo += f'if "%1"=="install" type nul > "{apt}"\r\n'
            if nome == "dpkg-query":
                corpo += f'if not exist "{apt}" exit /b 1\r\necho install ok installed\r\n'
            if nome == "systemctl":
                corpo += f'if "%1"=="is-active" if exist "{parado}" exit /b 3\r\n'
            (pasta / f"{nome}.cmd").write_text(corpo + "exit /b 0\r\n")
        else:
            corpo = f'#!/bin/sh\necho "{nome} $*" >> "{log}"\n'
            if nome == "apt-get":
                corpo += f'[ "$1" = install ] && : > "{apt}"\n'
            if nome == "dpkg-query":
                corpo += f'[ -e "{apt}" ] || exit 1\necho install ok installed\n'
            if nome == "systemctl":
                corpo += f'[ "$1" = is-active ] && [ -e "{parado}" ] && exit 3\n'
            f = pasta / nome
            f.write_text(corpo + "exit 0\n")
            f.chmod(0o755)
    return pasta


def instalar(raiz, bin_falso, respostas=(), *extras):
    env = dict(os.environ, PATH=str(bin_falso) + os.pathsep + os.environ["PATH"], PYTHONIOENCODING="utf-8",
               PYTHONDONTWRITEBYTECODE="1")
    env.pop("DISP_DB", None)
    return subprocess.run([sys.executable, str(PACOTE / "instalar.py"), "--raiz", str(raiz), *extras], env=env, text=True, encoding="utf-8",
                          input="".join(f"{r}\n" for r in respostas), capture_output=True, timeout=120)


def chamadas(bin_falso):
    """Lê e zera o registro: cada teste olha só o que a última execução chamou."""
    log = bin_falso / "chamadas.txt"
    texto = log.read_text()
    log.write_text("")
    return texto


def alteradoras(texto):
    """Chamadas que mudam algo no sistema (consultas como dpkg-query e is-active não contam)."""
    return [l.strip() for l in texto.splitlines() if l.strip() and "dpkg-query" not in l and "is-active" not in l and "hostname" not in l]


def primeira(url, token=TOKEN, usuario="ana", senha=SENHA):
    """Na ordem das perguntas: IP, URL e token do Zabbix, verificar TLS, IP do frontend, TOPdesk vazio,
    usuário e senha do painel, início da medição."""
    r = ["10.1.2.3", url, token, "n", "10.9.9.9", "", usuario, senha]
    return r + ["2026-07-01"] if token else r


def itens(raiz):
    c = sqlite3.connect(raiz / "etc/relatorios-engie/disponibilidade.db")
    try:
        return ([r[0] for r in c.execute("SELECT item FROM objetos")], c.execute("SELECT count(*) FROM incidentes").fetchone()[0])
    finally:
        c.close()


def test_primeira_instalacao_completa(tmp_path, bin_falso, zabbix):
    raiz = tmp_path / "raiz"
    r = instalar(raiz, bin_falso, primeira(zabbix))
    assert r.returncode == 0, r.stdout + r.stderr
    dest = raiz / "etc/relatorios-engie"

    env = (dest / "env").read_text(encoding="utf-8")
    assert f"ZABBIX_URL={zabbix}" in env and f"ZABBIX_TOKEN='{TOKEN}'" in env and "ZABBIX_VERIFY_TLS=0" in env
    assert "SERVIDOR_IP=10.1.2.3" in env and "ZABBIX_FRONTEND_IP=10.9.9.9" in env
    assert "TOPDESK" not in env
    if os.name != "nt":
        assert stat.S_IMODE((dest / "env").stat().st_mode) == 0o600
        assert (dest / "atualizar.sh").stat().st_mode & stat.S_IXUSR

    for arq in ("admin.py", "disp.py", "api.py", "relatorio.py", "schema.sql", "relatorios.json", "admin_web/app.js",
                "testes/test_disp.py", "criar_triggers_tid.py"):
        assert (dest / arq).exists(), arq
    assert "10.9.9.9 127.0.0.1" in (raiz / "etc/hosts").read_text()

    unidades = {u.name: u.read_text(encoding="utf-8") for u in (raiz / "etc/systemd/system").iterdir()}
    assert len(unidades) == 8
    assert not any("10.1.2.3" in t or "@IP@" in t for t in unidades.values())   # o IP fica só no env
    for u in ("api", "admin"):
        assert "EnvironmentFile=/etc/relatorios-engie/env" in unidades[f"relatorios-engie-{u}.service"]
    dashboards = list((dest / "grafana").glob("*.json"))
    assert len(dashboards) == 3 and not any(d.name.startswith("modelo") for d in dashboards)
    for d in dashboards:
        texto = d.read_text(encoding="utf-8")
        assert "__API_URL__" not in texto and "http://10.1.2.3:8099/" in texto
        json.loads(texto)
    assert all("America/Sao_Paulo" in unidades[f"relatorios-engie-{n}.timer"] for n in ("parciais", "fechamento"))

    assert itens(raiz) == (["GA_UHSA"], 1)                    # a carga inicial rodou de verdade contra o Zabbix falso
    assert "ana" in json.loads((dest / "admin_usuarios.json").read_text(encoding="utf-8"))

    chamadas = (bin_falso / "chamadas.txt").read_text()   # nome local: só aqui a lista inteira importa
    assert "apt-get install" in chamadas and "weasyprint" in chamadas and "n8n" not in chamadas and "docker" not in chamadas
    assert "systemctl daemon-reload" in chamadas and "systemctl enable --now relatorios-engie-atualizar.timer" in chamadas
    assert "systemctl enable --now relatorios-engie-admin.service" in chamadas and "restart" not in chamadas
    assert "Pendente: Corrija" not in r.stdout and "Pendente: Preencha o TOPdesk" in r.stdout
    assert "http://10.1.2.3:8098" in r.stdout


def test_atualizacao_nao_mexe_nos_dados(tmp_path, bin_falso, zabbix):
    raiz = tmp_path / "raiz"
    assert instalar(raiz, bin_falso, primeira(zabbix)).returncode == 0
    dest = raiz / "etc/relatorios-engie"
    env_antes = (dest / "env").read_bytes()
    usuarios_antes = (dest / "admin_usuarios.json").read_bytes()
    defs = json.loads((dest / "relatorios.json").read_text(encoding="utf-8"))
    defs["criado-no-painel"] = defs["tid"]
    (dest / "relatorios.json").write_text(json.dumps(defs), encoding="utf-8")
    (dest / "pdf").mkdir()
    (dest / "pdf/oficial.pdf").write_bytes(b"%PDF")
    (dest / "test_disp.py").write_text("antigo")                # sobra de versão antiga
    (dest / "disp.py").write_text("# versão velha")
    hosts_antes = (raiz / "etc/hosts").read_text()

    r = instalar(raiz, bin_falso)                               # sem nenhuma resposta: não pode perguntar nada
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Atualização" in r.stdout and "http://10.1.2.3:8098" in r.stdout
    assert (dest / "env").read_bytes() == env_antes
    assert (dest / "admin_usuarios.json").read_bytes() == usuarios_antes
    assert "criado-no-painel" in json.loads((dest / "relatorios.json").read_text(encoding="utf-8"))
    assert "criado-no-painel" not in json.loads((dest / "relatorios.json.pacote").read_text(encoding="utf-8"))
    assert (dest / "pdf/oficial.pdf").exists()
    assert itens(raiz) == (["GA_UHSA"], 1)
    assert not (dest / "test_disp.py").exists()
    assert "def calcular" in (dest / "disp.py").read_text(encoding="utf-8")   # programa foi trocado
    assert (raiz / "etc/hosts").read_text() == hosts_antes


@pytest.fixture
def instalado(tmp_path, bin_falso, zabbix):
    raiz = tmp_path / "raiz"
    r = instalar(raiz, bin_falso, primeira(zabbix))
    assert r.returncode == 0, r.stdout + r.stderr
    chamadas(bin_falso)
    return raiz, raiz / "etc/relatorios-engie"


def test_sem_mudanca_nao_mexe_em_nada(instalado, bin_falso):
    raiz, dest = instalado
    datas = {p: p.stat().st_mtime_ns for p in dest.rglob("*") if p.is_file() and "__pycache__" not in p.parts
             and not p.name.startswith(("disponibilidade.db", "admin_auditoria", "instalacao.json"))}
    r = instalar(raiz, bin_falso)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Já estava atualizado" in r.stdout and "Já instaladas" in r.stdout
    assert alteradoras(chamadas(bin_falso)) == []          # nem apt, nem daemon-reload, nem reinício
    assert {p: p.stat().st_mtime_ns for p in datas} == datas


def test_arquivo_do_painel_reinicia_so_o_painel(instalado, bin_falso):
    raiz, dest = instalado
    (dest / "admin.py").write_text("# velho", encoding="utf-8")
    r = instalar(raiz, bin_falso)
    assert "Atualizado: admin.py" in r.stdout and "Atualizado: disp.py" not in r.stdout
    assert alteradoras(chamadas(bin_falso)) == ["systemctl try-restart relatorios-engie-admin.service"]
    assert "def servidor" in (dest / "admin.py").read_text(encoding="utf-8")


def test_arquivo_comum_reinicia_os_dois(instalado, bin_falso):
    raiz, dest = instalado
    (dest / "disp.py").write_text("# velho", encoding="utf-8")
    instalar(raiz, bin_falso)
    assert sorted(alteradoras(chamadas(bin_falso))) == ["systemctl try-restart relatorios-engie-admin.service",
                                                       "systemctl try-restart relatorios-engie-api.service"]


def test_arquivo_da_tela_nao_reinicia_nada(instalado, bin_falso):
    raiz, dest = instalado
    (dest / "admin_web/app.js").write_text("// velho", encoding="utf-8")
    r = instalar(raiz, bin_falso)
    assert "Atualizado: admin_web/app.js" in r.stdout
    assert alteradoras(chamadas(bin_falso)) == []


def test_env_alterado_reinicia_api_e_painel(instalado, bin_falso):
    raiz, dest = instalado
    with (dest / "env").open("a", encoding="utf-8") as f:
        f.write("API_TOKEN=novo\n")
    instalar(raiz, bin_falso)
    assert len(alteradoras(chamadas(bin_falso))) == 2
    instalar(raiz, bin_falso)                               # na segunda vez já não há o que fazer
    assert alteradoras(chamadas(bin_falso)) == []


def test_unidade_alterada_recarrega_e_timer_desligado_nao_religa(instalado, bin_falso):
    raiz, _ = instalado
    (raiz / "etc/systemd/system/relatorios-engie-fechamento.timer").write_text("[Timer]\n", encoding="utf-8")
    (raiz / "etc/systemd/system/relatorios-engie-parciais.service").write_text("[Service]\n", encoding="utf-8")
    instalar(raiz, bin_falso)
    assert alteradoras(chamadas(bin_falso)) == ["systemctl daemon-reload", "systemctl try-restart relatorios-engie-fechamento.timer"]


def test_unidade_nova_e_ligada(instalado, bin_falso):
    raiz, _ = instalado
    (raiz / "etc/systemd/system/relatorios-engie-admin.service").unlink()
    instalar(raiz, bin_falso)
    assert alteradoras(chamadas(bin_falso)) == ["systemctl daemon-reload", "systemctl enable --now relatorios-engie-admin.service"]


def test_servico_parado_nao_e_religado_mas_avisa(instalado, bin_falso):
    raiz, dest = instalado
    (bin_falso / "parado").write_text("")
    r = instalar(raiz, bin_falso)
    assert "relatorios-engie-api.service não está no ar" in r.stdout
    assert alteradoras(chamadas(bin_falso)) == []


def test_dependencia_faltando_instala_so_ela(instalado, bin_falso):
    raiz, _ = instalado
    (bin_falso / "apt_rodou").unlink()
    r = instalar(raiz, bin_falso)
    assert "Instalando:" in r.stdout
    assert any(c.startswith("apt-get install") for c in alteradoras(chamadas(bin_falso)))


def test_arquivo_que_saiu_do_pacote_e_removido_e_dados_ficam(instalado, bin_falso):
    raiz, dest = instalado
    estado = json.loads((dest / "instalacao.json").read_text(encoding="utf-8"))
    (dest / "programa_velho.py").write_text("x")
    (dest / "meu_arquivo.txt").write_text("x")
    estado["arquivos"] += ["programa_velho.py", "../fora_da_pasta.txt"]
    (dest / "instalacao.json").write_text(json.dumps(estado), encoding="utf-8")
    fora = dest.parent / "fora_da_pasta.txt"
    fora.write_text("x")
    r = instalar(raiz, bin_falso)
    assert "Removido: programa_velho.py" in r.stdout and not (dest / "programa_velho.py").exists()
    assert (dest / "meu_arquivo.txt").exists() and fora.exists() and (dest / "env").exists()
    assert (dest / "disponibilidade.db").exists() and (dest / "admin_usuarios.json").exists()


def test_definicoes_sem_edicao_atualizam_e_com_edicao_ficam(instalado, bin_falso):
    raiz, dest = instalado
    defs, copia = dest / "relatorios.json", dest / "relatorios.json.pacote"
    original = defs.read_bytes()
    # Instalado de uma versão antiga e nunca editado: o estado guarda o hash exatamente desse conteúdo
    defs.write_text('{"antigo": {}}', encoding="utf-8")
    estado = json.loads((dest / "instalacao.json").read_text(encoding="utf-8"))
    estado["relatorios_sha"] = hashlib.sha256(defs.read_bytes()).hexdigest()
    (dest / "instalacao.json").write_text(json.dumps(estado), encoding="utf-8")
    r = instalar(raiz, bin_falso)
    assert "Atualizado: relatorios.json" in r.stdout and defs.read_bytes() == original and not copia.exists()
    # Editado pelo painel: fica, e a versão do pacote vai ao lado, avisando uma vez só
    defs.write_text('{"editado": {}}', encoding="utf-8")
    assert "foi mantido" in instalar(raiz, bin_falso).stdout
    assert defs.read_text(encoding="utf-8") == '{"editado": {}}' and copia.read_bytes() == original
    assert "foi mantido" not in instalar(raiz, bin_falso).stdout
    # Voltou a ser igual ao pacote: a cópia ao lado perde o sentido
    defs.write_bytes(original)
    instalar(raiz, bin_falso)
    assert not copia.exists()


def test_erro_no_meio_nao_esquece_o_reinicio(instalado, bin_falso):
    raiz, dest = instalado
    (dest / "admin.py").write_text("# velho", encoding="utf-8")
    (dest / "admin_usuarios.json").unlink()                 # sem usuário e sem resposta: para no meio
    assert instalar(raiz, bin_falso, ["ana", "curta"]).returncode != 0
    assert "try-restart" not in chamadas(bin_falso)
    assert "def servidor" in (dest / "admin.py").read_text(encoding="utf-8")     # o arquivo já foi trocado...
    r = instalar(raiz, bin_falso, ["ana", SENHA])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "systemctl try-restart relatorios-engie-admin.service" in chamadas(bin_falso)   # ...e o reinício não se perde


def test_forcar_copia_tudo_e_reinicia(instalado, bin_falso):
    raiz, _ = instalado
    r = instalar(raiz, bin_falso, (), "--forcar")
    assert r.returncode == 0 and "Atualizado: disp.py" in r.stdout
    feito = chamadas(bin_falso)
    assert "systemctl try-restart relatorios-engie-api.service" in feito and "systemctl try-restart relatorios-engie-admin.service" in feito
    assert "apt-get" not in feito


def test_dashboard_mudou_avisa_para_importar(instalado, bin_falso):
    raiz, dest = instalado
    (dest / "grafana/ENGIE_Disponibilidade_TID_sqlite.json").write_text("{}", encoding="utf-8")
    r = instalar(raiz, bin_falso)
    assert "Dashboard: ENGIE_Disponibilidade_TID_sqlite.json" in r.stdout and "importe de novo" in r.stdout
    assert "importe de novo" not in instalar(raiz, bin_falso).stdout


def test_mudar_ip_no_env_e_reinstalar(tmp_path, bin_falso, zabbix):
    raiz = tmp_path / "raiz"
    assert instalar(raiz, bin_falso, primeira(zabbix)).returncode == 0
    dest = raiz / "etc/relatorios-engie"
    hosts = raiz / "etc/hosts"
    hosts.write_text("127.0.0.1 localhost\n" + hosts.read_text() + "# 1.1.1.1 comentario\n")
    env = (dest / "env").read_text(encoding="utf-8")
    (dest / "env").write_text(env.replace("SERVIDOR_IP=10.1.2.3", "SERVIDOR_IP=10.7.7.7")
                              .replace("ZABBIX_FRONTEND_IP=10.9.9.9", "ZABBIX_FRONTEND_IP=10.8.8.8"), encoding="utf-8")
    r = instalar(raiz, bin_falso)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "http://10.7.7.7:8098" in r.stdout
    assert "http://10.7.7.7:8099/" in (dest / "grafana/ENGIE_Disponibilidade_TID_sqlite.json").read_text(encoding="utf-8")
    assert hosts.read_text().splitlines() == ["127.0.0.1 localhost", "10.8.8.8 127.0.0.1", "# 1.1.1.1 comentario"]


def test_instalacao_antiga_leva_o_ip_para_o_env(tmp_path, bin_falso, zabbix):
    raiz = tmp_path / "raiz"
    assert instalar(raiz, bin_falso, primeira(zabbix)).returncode == 0
    dest, unidade = raiz / "etc/relatorios-engie", raiz / "etc/systemd/system/relatorios-engie-api.service"
    env = dest / "env"
    env.write_text("".join(l + "\n" for l in env.read_text(encoding="utf-8").splitlines() if "SERVIDOR_IP" not in l), encoding="utf-8")
    unidade.write_text("[Service]\nEnvironment=API_HOST=10.5.5.5 API_PORT=8099\n", encoding="utf-8")
    r = instalar(raiz, bin_falso)                               # sem perguntas
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SERVIDOR_IP=10.5.5.5" in env.read_text(encoding="utf-8") and f"ZABBIX_TOKEN='{TOKEN}'" in env.read_text(encoding="utf-8")
    assert "API_HOST" not in unidade.read_text(encoding="utf-8")
    assert "http://10.5.5.5:8098" in r.stdout


def test_ip_invalido_e_recusado(tmp_path, bin_falso, zabbix):
    r = instalar(tmp_path / "raiz", bin_falso, ["nao-e-ip"])
    assert r.returncode != 0 and "IP válido" in r.stdout


@pytest.mark.parametrize("programa,args", [("api.py", []), ("admin.py", ["servidor"])])
def test_servicos_atendem_no_ip_do_env(tmp_path, bin_falso, programa, args):
    raiz = tmp_path / "raiz"
    assert instalar(raiz, bin_falso, ["127.0.0.2", "", "", "n", "", "", "ana", SENHA]).returncode == 0
    dest = raiz / "etc/relatorios-engie"
    with socket.socket() as s:
        s.bind(("127.0.0.2", 0))
        porta = s.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if k not in ("API_HOST", "ADMIN_HOST")}
    env.update(SERVIDOR_IP="127.0.0.2", API_PORT=str(porta), ADMIN_PORT=str(porta), DISP_DB=str(dest / "disponibilidade.db"),
               PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.Popen([sys.executable, programa, *args], cwd=dest, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.2", porta), 0.2):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise AssertionError("o serviço não atendeu no IP do env")
    finally:
        p.terminate()
        p.wait(10)


def test_sem_enderecos_do_ambiente_no_repositorio():
    proibidos = re.compile(r"172\.19\.|teltecsolutions")
    for p in PACOTE.rglob("*"):
        if (p.is_file() and not {".git", "dist", "__pycache__"} & set(p.parts) and p.suffix not in (".xlsx", ".pyc")
                and p.name not in ("test_instalar.py", "env.engie")):       # env.engie está no .gitignore
            assert not proibidos.search(p.read_text(encoding="utf-8", errors="ignore")), p


def test_env_exemplo_cobre_todas_as_variaveis():
    texto = (PACOTE / "servidor/env.exemplo").read_text(encoding="utf-8")
    documentadas = set(re.findall(r"^#?([A-Z][A-Z0-9_]+)=", texto, re.M))
    usadas = set()
    for arq in ("servidor/disp.py", "servidor/api.py", "servidor/relatorio.py", "servidor/admin.py", "instalar.py"):
        fonte = (PACOTE / arq).read_text(encoding="utf-8")
        usadas |= set(re.findall(r"""getenv\(["']([A-Z0-9_]+)""", fonte)) | set(re.findall(r"""v\[["']([A-Z0-9_]+)["']\]""", fonte))
    campos = re.search(r"ENV_CAMPOS = \{(.*?)\n\}", (PACOTE / "servidor/admin.py").read_text(encoding="utf-8"), re.S).group(1)
    usadas |= set(re.findall(r'^\s+"([A-Z0-9_]+)":', campos, re.M))
    # definidas nas unidades systemd ou só para sobrepor SERVIDOR_IP, de propósito fora do exemplo
    assert usadas - {"API_HOST", "API_PORT", "ADMIN_HOST", "ADMIN_PORT"} <= documentadas, usadas - documentadas


def test_instalar_aproveita_env_copiado_antes(tmp_path, bin_falso):
    """Env preenchido à mão antes da instalação: nada de perguntas sobre ele, e o IP vem de lá."""
    raiz = tmp_path / "raiz"
    dest = raiz / "etc/relatorios-engie"
    dest.mkdir(parents=True)
    texto = (PACOTE / "servidor/env.exemplo").read_text(encoding="utf-8")
    (dest / "env").write_text(texto.replace("DISP_DB=/etc/relatorios-engie/disponibilidade.db", f"DISP_DB={dest / 'disponibilidade.db'}")
                              .replace("#ZABBIX_FRONTEND_IP", "ZABBIX_FRONTEND_IP"), encoding="utf-8")
    r = instalar(raiz, bin_falso, ["ana", SENHA])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "http://192.0.2.10:8098" in r.stdout
    assert "192.0.2.20 zabbix.exemplo.com.br" in (raiz / "etc/hosts").read_text()
    assert "http://192.0.2.10:8099/" in (dest / "grafana/ENGIE_Disponibilidade_TID_sqlite.json").read_text(encoding="utf-8")


def test_sem_token_instala_e_avisa(tmp_path, bin_falso, zabbix):
    raiz = tmp_path / "raiz"
    r = instalar(raiz, bin_falso, primeira(zabbix, token=""))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Pendente: Preencha a URL e o token do Zabbix" in r.stdout and "Carga inicial\n" not in r.stdout
    assert (raiz / "etc/systemd/system/relatorios-engie-admin.service").exists()


def test_zabbix_fora_do_ar_nao_impede_a_instalacao(tmp_path, bin_falso):
    raiz = tmp_path / "raiz"
    r = instalar(raiz, bin_falso, primeira("http://127.0.0.1:9"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Pendente: Corrija o problema acima" in r.stdout
    assert (raiz / "etc/systemd/system/relatorios-engie-admin.service").exists()


def test_senha_curta_e_recusada(tmp_path, bin_falso, zabbix):
    r = instalar(tmp_path / "raiz", bin_falso, primeira(zabbix, senha="curta"))
    assert r.returncode != 0 and "10 caracteres" in r.stdout


def test_token_errado_vira_pendencia(tmp_path, bin_falso, zabbix):
    r = instalar(tmp_path / "raiz", bin_falso, primeira(zabbix, token="errado"))
    assert r.returncode == 0 and "token inválido" in (r.stdout + r.stderr) and "Pendente" in r.stdout


def test_pacote_sem_n8n():
    for arq in ["instalar.py", "instalar.sh", "README.md", "docs/instalacao.md", "docs/operacao.md"]:
        assert "n8n" not in (PACOTE / arq).read_text(encoding="utf-8").lower(), arq
    for p in (PACOTE / "servidor").rglob("*"):
        if p.is_file() and p.suffix in (".py", ".js", ".sql", ".service", ".timer", ".sh") and "testes" not in p.parts:
            assert "n8n" not in p.read_text(encoding="utf-8").lower(), p
