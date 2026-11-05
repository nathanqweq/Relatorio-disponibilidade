#!/usr/bin/env python3
"""
Instala ou atualiza os Relatórios de Disponibilidade ENGIE. Use pelo instalar.sh, como root.

Primeira vez: instala as dependências, copia os programas, pergunta as credenciais, cria o usuário
do painel, faz a carga inicial e liga os serviços.
Atualização: rodar de novo compara o pacote com o que está instalado e mexe só no que mudou: copia
os arquivos diferentes, instala só as dependências que faltam e reinicia só o serviço afetado.
Credenciais (env), banco, PDFs, usuários, backups e a auditoria nunca são alterados. Serviço ou timer
desligado de propósito continua desligado.
Mudou um IP no env? Rode de novo: refaz o /etc/hosts e os dashboards e reinicia a API e o painel.

--forcar   copia tudo de novo e reinicia os serviços, mesmo sem diferença.
--raiz PASTA instala dentro de uma pasta em vez de / (usado pelos testes).
"""
import argparse
import getpass
import hashlib
import ipaddress
import json
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
# Arquivos que cada serviço carrega na memória: mudou algum, o serviço precisa reiniciar.
# O admin_web fica de fora de propósito, porque o painel lê esses arquivos a cada pedido.
CARREGA = {"relatorios-engie-api.service": {"api.py", "disp.py", "relatorio.py", "schema.sql"},
           "relatorios-engie-admin.service": {"admin.py", "api.py", "disp.py", "schema.sql"}}
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


def resumo_hash(caminho):
    return hashlib.sha256(caminho.read_bytes()).hexdigest() if caminho.exists() else None


def igual(a, b):
    return b.exists() and a.read_bytes() == b.read_bytes()


def arquivos_do_pacote():
    """Programas do pacote, como caminhos relativos. relatorios.json tem regra própria."""
    return sorted(p.relative_to(ORIGEM).as_posix() for p in ORIGEM.rglob("*")
                  if p.is_file() and p.name != "relatorios.json" and p.suffix != ".pyc"
                  and "__pycache__" not in p.parts and ".pytest_cache" not in p.parts)


class Instalacao:
    def __init__(self, raiz, forcar=False):
        self.raiz = Path(raiz)
        self.forcar = forcar
        self.dest = self.raiz / "etc/relatorios-engie"
        self.systemd = self.raiz / "etc/systemd/system"
        self.hosts = self.raiz / "etc/hosts"
        self.env = self.dest / "env"
        self.arq_estado = self.dest / "instalacao.json"
        self.pendencias = []
        self.alteracoes = 0
        self.mudados = set()

    def ler_estado(self):
        try:
            return json.loads(self.arq_estado.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def gravar_estado(self, **novo):
        """Tem o hash do env: só root lê. Gravado em duas etapas para um erro no meio não esquecer um reinício."""
        dados = {**self.ler_estado(), **novo}
        tmp = self.arq_estado.with_name("instalacao.json.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(dados, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.arq_estado)

    def contar(self, mensagem):
        self.alteracoes += 1
        print(f"  {mensagem}")

    def dependencias(self):
        titulo("Dependências")
        faltam = []
        for pacote in DEPENDENCIAS:
            try:
                r = rodar("dpkg-query", "-W", "-f=${Status}", pacote, capture_output=True, text=True)
                instalado = r.returncode == 0 and "install ok installed" in r.stdout
            except OSError:
                instalado = False
            if not instalado:
                faltam.append(pacote)
        if not faltam:
            print("  Já instaladas.")
            return
        self.contar(f"Instalando: {', '.join(faltam)}")
        amb = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
        rodar("apt-get", "update", "-qq", env=amb)
        if rodar("apt-get", "install", "-y", "-qq", *faltam, env=amb).returncode != 0:
            sys.exit("Não foi possível instalar as dependências: confira o acesso aos repositórios do apt.")

    def atualizar_programas(self):
        titulo("Programas")
        self.dest.mkdir(parents=True, exist_ok=True)
        estado = self.ler_estado()
        pacote = arquivos_do_pacote()
        antes = self.alteracoes
        for rel in pacote:
            origem, destino = ORIGEM / rel, self.dest / rel
            if self.forcar or not igual(origem, destino):
                destino.parent.mkdir(parents=True, exist_ok=True)
                self.contar(f"{'Novo' if not destino.exists() else 'Atualizado'}: {rel}")
                shutil.copy2(origem, destino)
                self.mudados.add(rel)
        # Remove só o que uma versão anterior instalou e saiu do pacote; dados do servidor nunca estão nesta lista.
        for rel in sorted((set(estado.get("arquivos", [])) | set(ANTIGOS)) - set(pacote)):
            alvo = (self.dest / rel).resolve()
            if self.dest.resolve() in alvo.parents and alvo.is_file():
                alvo.unlink()
                self.contar(f"Removido: {rel}")
        os.chmod(self.dest / "atualizar.sh", 0o755)
        self.atualizar_definicoes(estado)
        # Erro daqui em diante: a próxima execução ainda sabe que estes arquivos pedem reinício.
        self.mudados |= set(estado.get("reiniciar_por", []))
        self.gravar_estado(arquivos=pacote, reiniciar_por=sorted(self.mudados))
        if self.alteracoes == antes:
            print("  Nada mudou.")

    def atualizar_definicoes(self, estado):
        """O painel edita relatorios.json: só é trocado se ninguém mexeu desde a última instalação."""
        defs, novas, copia = self.dest / "relatorios.json", ORIGEM / "relatorios.json", self.dest / "relatorios.json.pacote"
        base = estado.get("relatorios_sha")
        if not defs.exists() or (base and resumo_hash(defs) == base and not igual(novas, defs)):
            self.contar(f"{'Novo' if not defs.exists() else 'Atualizado'}: relatorios.json")
            shutil.copy2(novas, defs)
            base = resumo_hash(novas)
        if igual(novas, defs):
            copia.unlink(missing_ok=True)
            base = resumo_hash(novas)
        elif not igual(novas, copia):
            self.contar("relatorios.json tem edições suas e foi mantido. A versão do pacote está em relatorios.json.pacote.")
            shutil.copy2(novas, copia)
        if base:
            self.gravar_estado(relatorios_sha=base)

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
            self.contar(f"/etc/hosts: {nome} aponta para {ip}")

    def gerar_dashboards(self, ip, primeira_vez):
        """O Grafana não lê o env: os JSONs do repositório têm um marcador, trocado aqui pelo endereço da API."""
        origem = PACOTE / "grafana"
        destino = self.dest / "grafana"
        destino.mkdir(parents=True, exist_ok=True)
        host = f"[{ip}]" if ":" in ip else ip
        mudou = False
        for arq in sorted(origem.glob("ENGIE_*.json")):
            alvo = destino / arq.name
            novo = arq.read_text(encoding="utf-8").replace(MARCADOR_API, f"http://{host}:8099").encode("utf-8")
            if self.forcar or not alvo.exists() or alvo.read_bytes() != novo:
                alvo.write_bytes(novo)
                self.contar(f"Dashboard: {arq.name}")
                mudou = True
        if mudou and not primeira_vez:
            self.pendencias.append(f"Os dashboards mudaram: importe de novo os arquivos de {destino} no Grafana.")

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
        novas, alteradas = [], []
        for u in sorted((ORIGEM / "systemd").iterdir()):
            if u.suffix in (".service", ".timer"):
                alvo = self.systemd / u.name
                if not alvo.exists():
                    novas.append(u.name)
                elif self.forcar or not igual(u, alvo):
                    alteradas.append(u.name)
                else:
                    continue
                shutil.copy2(u, alvo)
                self.contar(f"{'Nova' if u.name in novas else 'Atualizada'}: {u.name}")
        if novas or alteradas:
            rodar("systemctl", "daemon-reload")

        falhas = []
        # Só liga o que é novo: o que já existia e está desligado foi desligado por alguém.
        for u in SEMPRE_LIGADOS + TIMERS:
            if u in novas and rodar("systemctl", "enable", "--now", u).returncode != 0:
                falhas.append(u)

        env_mudou = resumo_hash(self.env) != self.ler_estado().get("env_sha")
        reiniciar = []
        for u in SEMPRE_LIGADOS:
            if u not in novas and (self.forcar or env_mudou or u in alteradas or CARREGA[u] & self.mudados):
                reiniciar.append(u)
        reiniciar += [u for u in TIMERS if u in alteradas]      # só assim o timer usa o novo horário
        for u in reiniciar:
            # try-restart não religa um serviço que alguém deixou parado
            if rodar("systemctl", "try-restart", u).returncode != 0:
                falhas.append(u)
            else:
                self.contar(f"Reiniciado: {u}")
        for u in SEMPRE_LIGADOS:
            if u not in falhas and rodar("systemctl", "is-active", "--quiet", u).returncode != 0:
                self.pendencias.append(f"{u} não está no ar. Se não foi de propósito: systemctl start {u}")
        for u in falhas:
            self.pendencias.append(f"Serviço com problema: {u} (veja com journalctl -u {u})")
        if not (novas or alteradas or reiniciar):
            print("  Nada mudou.")

    def executar(self):
        primeira_vez = not self.env.exists()
        titulo("Primeira instalação" if primeira_vez else "Atualização")
        self.dependencias()
        ip = self.ip_conhecido()               # lido antes de trocar as unidades
        self.atualizar_programas()
        if not ip:
            ip = self.perguntar_ip(self.ip_detectado())
        tem_credenciais = self.configurar_env(ip)
        self.aplicar_hosts()
        self.gerar_dashboards(ip, primeira_vez)
        rodar(sys.executable, "disp.py", "init", cwd=self.dest, env=dict(self.amb_servidor(), DISP_DB=str(self.db())),
              stdout=subprocess.DEVNULL)
        self.criar_usuario()
        if primeira_vez and tem_credenciais:
            self.carga_inicial()
        self.instalar_unidades()
        self.gravar_estado(reiniciar_por=[], env_sha=resumo_hash(self.env))
        titulo("Pronto")
        if not self.alteracoes:
            print("  Já estava atualizado: nada foi alterado.")
        else:
            print(f"  {self.alteracoes} alteração(ões) aplicada(s).")
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
    ap.add_argument("--forcar", action="store_true", help="copia tudo de novo e reinicia os serviços, mesmo sem diferença")
    a = ap.parse_args()
    if a.raiz == "/":
        if os.geteuid() != 0:
            sys.exit("Rode como root.")
        if not shutil.which("apt-get") or not shutil.which("systemctl"):
            sys.exit("Este instalador é para Debian com systemd.")
    Instalacao(a.raiz, a.forcar).executar()


if __name__ == "__main__":
    main()
