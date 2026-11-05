#!/usr/bin/env python3
"""
Instala ou atualiza os Relatórios de Disponibilidade ENGIE. Use pelo instalar.sh, como root.

Primeira vez: instala as dependências, copia os programas, pergunta as credenciais, cria o usuário
do painel, faz a carga inicial e liga os serviços.
Atualização: rodar de novo troca só os programas e as unidades. Credenciais (env), banco, PDFs,
usuários, backups e a auditoria nunca são alterados.
Mudou um IP no env? Rode de novo: refaz o /etc/hosts e os dashboards e reinicia os serviços.

--raiz PASTA instala dentro de uma pasta em vez de / (usado pelos testes).
"""
import argparse
import getpass
import ipaddress
import os
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

PACOTE = Path(__file__).resolve().parent
ORIGEM = PACOTE / "servidor"
MARCADOR_API = "__API_URL__"
SEMPRE_LIGADOS = ["relatorios-engie-api.service", "relatorios-engie-admin.service"]
TIMERS = ["relatorios-engie-atualizar.timer", "relatorios-engie-parciais.timer", "relatorios-engie-fechamento.timer"]
DEPENDENCIAS = ["python3-requests", "sqlite3", "weasyprint"]
# Arquivos de versões anteriores que mudaram de lugar ou deixaram de existir.
ANTIGOS = ["test_disp.py", "test_admin.py", "README.md", "systemd/LEIA-ME.txt"]
IGNORAR = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", "relatorios.json")
INTERATIVO = sys.stdin.isatty()


def titulo(texto):
    print(f"\n=== {texto}", flush=True)


def perguntar(texto, padrao="", segredo=False):
    sufixo = f" [{padrao}]" if padrao else ""
    try:
        if segredo and INTERATIVO:
            r = getpass.getpass(f"{texto}{sufixo}: ")
        else:
            r = input(f"{texto}{sufixo}: ")
    except EOFError:
        return padrao
    return r.strip() or padrao


def perguntar_valor(texto, padrao="", segredo=False):
    """O env é lido pelo systemd e pelo shell: aspas simples e quebras de linha não são aceitas."""
    while True:
        v = perguntar(texto, padrao, segredo)
        if "'" not in v and not re.search(r"[\x00-\x1f\x7f]", v):
            return v
        print("  Não use aspas simples nesse valor.")


def sim(texto, padrao):
    return perguntar(f"{texto} (s/n)", "s" if padrao else "n").lower().startswith("s")


def valor_env(v):
    return v if re.fullmatch(r"[A-Za-z0-9_./:@%+=,-]*", v) else f"'{v}'"


def rodar(*cmd, **kw):
    cmd = [shutil.which(cmd[0]) or cmd[0], *cmd[1:]]
    return subprocess.run(cmd, check=False, **kw)


class Instalacao:
    def __init__(self, raiz):
        self.raiz = Path(raiz)
        self.dest = self.raiz / "etc/relatorios-engie"
        self.systemd = self.raiz / "etc/systemd/system"
        self.hosts = self.raiz / "etc/hosts"
        self.env = self.dest / "env"
        self.pendencias = []

    def dependencias(self):
        titulo("Dependências")
        amb = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
        rodar("apt-get", "update", "-qq", env=amb)
        if rodar("apt-get", "install", "-y", "-qq", *DEPENDENCIAS, env=amb).returncode != 0:
            sys.exit("Não foi possível instalar as dependências: confira o acesso aos repositórios do apt.")

    def copiar_programas(self):
        titulo("Programas")
        self.dest.mkdir(parents=True, exist_ok=True)
        shutil.copytree(ORIGEM, self.dest, dirs_exist_ok=True, ignore=IGNORAR)
        for nome in ANTIGOS:
            (self.dest / nome).unlink(missing_ok=True)
        os.chmod(self.dest / "atualizar.sh", 0o755)
        # O painel edita relatorios.json: uma atualização não pode apagar essas edições.
        defs, novas = self.dest / "relatorios.json", ORIGEM / "relatorios.json"
        if not defs.exists():
            shutil.copy2(novas, defs)
        elif defs.read_bytes() != novas.read_bytes():
            shutil.copy2(novas, self.dest / "relatorios.json.pacote")
            print("  relatorios.json foi mantido. A versão do pacote está em relatorios.json.pacote.")
        print(f"  Copiados para {self.dest}")

    def ler_env(self):
        out = {}
        if self.env.exists():
            for linha in self.env.read_text(encoding="utf-8").splitlines():
                m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$", linha)
                if m and not linha.lstrip().startswith("#"):
                    out[m.group(1)] = m.group(2).strip().strip("'\"")
        return out

    def ip_conhecido(self):
        """IP já definido: no env ou, em instalações antigas, na unidade da API."""
        if self.ler_env().get("SERVIDOR_IP"):
            return self.ler_env()["SERVIDOR_IP"]
        unidade = self.systemd / SEMPRE_LIGADOS[0]
        if unidade.exists():
            m = re.search(r"API_HOST=(\S+)", unidade.read_text(encoding="utf-8"))
            if m:
                return m.group(1)
        return ""

    def ip_detectado(self):
        try:
            saida = rodar("hostname", "-I", capture_output=True, text=True).stdout.split()
        except OSError:
            saida = []
        return saida[0] if saida and re.fullmatch(r"[0-9a-fA-F.:]+", saida[0]) else ""

    def perguntar_ip(self, padrao):
        while True:
            ip = perguntar_valor("IP deste servidor para a API e o painel", padrao)
            try:
                ipaddress.ip_address(ip)
                return ip
            except ValueError:
                print("  Informe um IP válido.")
                if not INTERATIVO:
                    sys.exit("Sem o IP deste servidor não dá para continuar.")

    def acrescentar_env(self, chave, valor):
        """Instalação antiga: acrescenta a variável nova sem tocar nas demais linhas do env."""
        texto = self.env.read_text(encoding="utf-8")
        with self.env.open("a", encoding="utf-8", newline="\n") as f:
            f.write(("" if not texto or texto.endswith("\n") else "\n") + f"{chave}={valor_env(valor)}\n")
        print(f"  {chave} acrescentado ao env.")

    def configurar_env(self, ip):
        if self.env.exists():
            print("  Credenciais mantidas (arquivo env já existe).")
            if not self.ler_env().get("SERVIDOR_IP"):
                self.acrescentar_env("SERVIDOR_IP", ip)
            return False
        titulo("Credenciais (tudo pode ser mudado depois no painel, em Configuração)")
        v = {"DISP_DB": str(self.dest / "disponibilidade.db"), "SERVIDOR_IP": ip}
        v["ZABBIX_URL"] = perguntar_valor("URL do Zabbix (ex.: https://zabbix.exemplo.com.br)")
        v["ZABBIX_TOKEN"] = perguntar_valor("Token de API do Zabbix", segredo=True)
        if not sim("Verificar o certificado do Zabbix", False):
            v["ZABBIX_VERIFY_TLS"] = "0"
        v["ZABBIX_FRONTEND_IP"] = perguntar_valor("IP do frontend do Zabbix, para o /etc/hosts (vazio se o nome já resolve)")
        v["TOPDESK_URL"] = perguntar_valor("URL do TOPdesk (vazio para configurar depois)")
        if v["TOPDESK_URL"]:
            v["TOPDESK_USER"] = perguntar_valor("Usuário do TOPdesk")
            v["TOPDESK_APP_PASSWORD"] = perguntar_valor("Senha de aplicação do TOPdesk", segredo=True)
            if not sim("Verificar o certificado do TOPdesk", True):
                v["TOPDESK_VERIFY_TLS"] = "0"
        linhas = [f"{k}={valor_env(x)}" for k, x in v.items() if x]
        fd = os.open(self.env, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(linhas) + "\n")
        os.chmod(self.env, 0o600)
        if not v["ZABBIX_URL"] or not v["ZABBIX_TOKEN"]:
            self.pendencias.append("Preencha a URL e o token do Zabbix em Configuração e rode a Carga inicial em Tarefas.")
        if not v["TOPDESK_URL"]:
            self.pendencias.append("Preencha o TOPdesk em Configuração, para os chamados entrarem nos relatórios.")
        return bool(v["ZABBIX_URL"] and v["ZABBIX_TOKEN"])

    def aplicar_hosts(self):
        """Mantém no /etc/hosts o nome do Zabbix apontando para ZABBIX_FRONTEND_IP. Só mexe na linha desse nome."""
        env = self.ler_env()
        nome, ip = urlparse(env.get("ZABBIX_URL", "")).hostname, env.get("ZABBIX_FRONTEND_IP")
        if not nome or not ip:
            return
        linhas = self.hosts.read_text(encoding="utf-8").splitlines() if self.hosts.exists() else []
        novas = [f"{ip} {nome}" if re.fullmatch(rf"\s*\S+\s+{re.escape(nome)}\s*", l) else l for l in linhas]
        if not any(re.search(rf"\s{re.escape(nome)}(\s|$)", l) and not l.lstrip().startswith("#") for l in novas):
            novas.append(f"{ip} {nome}")
        if novas != linhas:
            self.hosts.parent.mkdir(parents=True, exist_ok=True)
            self.hosts.write_text("\n".join(novas) + "\n", encoding="utf-8", newline="\n")
            print(f"  /etc/hosts: {nome} aponta para {ip}")

    def gerar_dashboards(self, ip):
        """O Grafana não lê o env: os JSONs do repositório têm um marcador, trocado aqui pelo endereço da API."""
        origem = PACOTE / "grafana"
        destino = self.dest / "grafana"
        destino.mkdir(parents=True, exist_ok=True)
        host = f"[{ip}]" if ":" in ip else ip
        for arq in sorted(origem.glob("ENGIE_*.json")):
            texto = arq.read_text(encoding="utf-8").replace(MARCADOR_API, f"http://{host}:8099")
            (destino / arq.name).write_text(texto, encoding="utf-8", newline="\n")
        print(f"  Dashboards para importar no Grafana: {destino}")

    def amb_servidor(self):
        return dict(os.environ, PYTHONIOENCODING="utf-8")

    def criar_usuario(self):
        if (self.dest / "admin_usuarios.json").exists():
            return
        titulo("Usuário do painel")
        while True:
            nome = perguntar("Nome de usuário", "admin")
            senha = perguntar("Senha (mínimo de 10 caracteres)", segredo=True)
            r = rodar(sys.executable, "admin.py", "usuario", nome, cwd=self.dest, env=self.amb_servidor(),
                      input=senha + "\n", text=True, encoding="utf-8", capture_output=True)
            if r.returncode == 0:
                print(f"  {r.stdout.strip()}")
                return
            print(f"  {(r.stderr or r.stdout).strip()}")
            if not INTERATIVO:
                sys.exit("Não foi possível criar o usuário do painel.")

    def carga_inicial(self):
        titulo("Carga inicial")
        desde = perguntar("Primeiro dia a medir (AAAA-MM-DD)", date.today().replace(day=1).isoformat())
        r = rodar(sys.executable, "admin.py", "carga-inicial", desde, cwd=self.dest, env=self.amb_servidor())
        if r.returncode != 0:
            self.pendencias.append("Corrija o problema acima e rode a Carga inicial em Tarefas, no painel.")

    def instalar_unidades(self):
        titulo("Serviços")
        self.systemd.mkdir(parents=True, exist_ok=True)
        for u in sorted((ORIGEM / "systemd").iterdir()):
            if u.suffix in (".service", ".timer"):
                shutil.copy2(u, self.systemd / u.name)
        rodar("systemctl", "daemon-reload")
        falhas = [u for u in SEMPRE_LIGADOS + TIMERS if rodar("systemctl", "enable", u).returncode != 0]
        # restart: numa atualização, os serviços precisam carregar o código novo
        falhas += [u for u in SEMPRE_LIGADOS if rodar("systemctl", "restart", u).returncode != 0]
        falhas += [u for u in TIMERS if rodar("systemctl", "start", u).returncode != 0]
        for u in sorted(set(falhas)):
            self.pendencias.append(f"Serviço com problema: {u} (veja com journalctl -u {u})")
        print("  Serviços e timers instalados.")

    def executar(self):
        primeira_vez = not self.env.exists()
        titulo("Primeira instalação" if primeira_vez else "Atualização")
        self.dependencias()
        ip = self.ip_conhecido()               # lido antes de trocar as unidades
        self.copiar_programas()
        if not ip:
            ip = self.perguntar_ip(self.ip_detectado())
        tem_credenciais = self.configurar_env(ip)
        self.aplicar_hosts()
        self.gerar_dashboards(ip)
        rodar(sys.executable, "disp.py", "init", cwd=self.dest, env=dict(self.amb_servidor(), DISP_DB=str(self.db())),
              stdout=subprocess.DEVNULL)
        self.criar_usuario()
        if primeira_vez and tem_credenciais:
            self.carga_inicial()
        self.instalar_unidades()
        titulo("Pronto")
        print(f"  Painel: http://{ip}:8098")
        for p in self.pendencias:
            print(f"  Pendente: {p}")
        if primeira_vez:
            print("  Próximos passos: docs/instalacao.md (tags no Zabbix e dashboards no Grafana).")

    def db(self):
        return self.ler_env().get("DISP_DB") or self.dest / "disponibilidade.db"


def main():
    ap = argparse.ArgumentParser(description="Instala ou atualiza os Relatórios de Disponibilidade ENGIE")
    ap.add_argument("--raiz", default="/", help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.raiz == "/":
        if os.geteuid() != 0:
            sys.exit("Rode como root.")
        if not shutil.which("apt-get") or not shutil.which("systemctl"):
            sys.exit("Este instalador é para Debian com systemd.")
    Instalacao(a.raiz).executar()


if __name__ == "__main__":
    main()
