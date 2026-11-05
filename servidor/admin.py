#!/usr/bin/env python3
"""
Painel web de administração dos Relatórios de Disponibilidade ENGIE.

Serviço separado da API (api.py continua somente leitura e sem login). Só usa a biblioteca padrão.
As alterações passam pelos comandos que já existem (disp.py, relatorio.py), que continuam sendo
a única implementação das regras; toda ação fica registrada em admin_auditoria.log.

    python3 admin.py usuario NOME           cria o usuário ou troca a senha dele
    python3 admin.py servidor               sobe o painel (padrão se nenhum comando for dado)
    python3 admin.py testar zabbix|topdesk  testa a credencial (usado pelo painel)
    python3 admin.py carga-inicial AAAA-MM-DD   cadastra itens e importa o histórico desde a data

Variáveis do serviço: SERVIDOR_IP (onde atender; ADMIN_HOST tem prioridade; padrão 127.0.0.1), ADMIN_PORT (padrão 8098),
                      ADMIN_TLS_CERT e ADMIN_TLS_KEY (opcionais: servem o painel em HTTPS)
"""
import argparse
import base64
import calendar
import collections
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import ssl
import string
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import api   # noqa: E402
import disp  # noqa: E402

WEB = BASE / "admin_web"
ENV_FILE = BASE / "env"
USUARIOS_FILE = BASE / "admin_usuarios.json"
LOG_FILE = BASE / "admin_auditoria.log"
BACKUPS = BASE / "backups"
DEFS = BASE / "relatorios.json"

COOKIE = "engie_admin"
SESSAO_SEG = 8 * 3600
TENTATIVAS_MAX, BLOQUEIO_SEG = 5, 300
PBKDF2_ITER = 600_000
SENHA_MIN = 10
CORPO_MAX = 2 * 1024 * 1024
TAREFA_MAX_SEG = 2 * 3600
TAREFA_MAX_LINHAS = 20_000
TAREFAS_GUARDADAS = 50
MESES_GUARDADOS = 3   # o mesmo "limpar --meses" do atualizar.sh: antes disso, as quedas já foram apagadas

ESTATICOS = {"/": ("index.html", "text/html; charset=utf-8"),
             "/app.js": ("app.js", "text/javascript; charset=utf-8"),
             "/style.css": ("style.css", "text/css; charset=utf-8")}

# Só estas variáveis podem ser alteradas pelo painel. As demais linhas do env ficam como estão.
ENV_CAMPOS = {
    "DISP_DB": {"rotulo": "Banco SQLite", "ajuda": "Caminho completo do arquivo do banco."},
    "PDF_DIR": {"rotulo": "Pasta dos PDFs", "ajuda": "Vazio = pasta pdf ao lado dos programas."},
    "ZABBIX_URL": {"rotulo": "URL do Zabbix", "ajuda": "Frontend, com https://", "url": True},
    "ZABBIX_TOKEN": {"rotulo": "Token do Zabbix", "segredo": True},
    "ZABBIX_VERIFY_TLS": {"rotulo": "Verificar certificado do Zabbix", "ajuda": "1 = sim, 0 = não", "bool": True},
    "TOPDESK_URL": {"rotulo": "URL do TOPdesk", "ajuda": "Com https://", "url": True},
    "TOPDESK_USER": {"rotulo": "Usuário do TOPdesk"},
    "TOPDESK_APP_PASSWORD": {"rotulo": "Senha de aplicação do TOPdesk", "segredo": True},
    "TOPDESK_VERIFY_TLS": {"rotulo": "Verificar certificado do TOPdesk", "ajuda": "1 = sim, 0 = não", "bool": True},
    "CHAMADO_REGEX": {"rotulo": "Regex do número do chamado", "ajuda": "Opcional. O 1º grupo é o número.", "regex": True},
    "API_TOKEN": {"rotulo": "Token da API (porta 8099)", "ajuda": "Opcional. Se definir, configure o Grafana com o mesmo token.",
                  "segredo": True},
    "GRAFANA_URL": {"rotulo": "URL do Grafana", "ajuda": "Vira atalho no painel.", "url": True},
}

# Unidades systemd que o painel pode ver e controlar (nunca se aceita outro nome).
SERVICO, TIMER, AVULSO, PROPRIO = "servico", "timer", "avulso", "proprio"
UNIDADES = {
    "relatorios-engie-api.service": {"titulo": "API de disponibilidade", "tipo": SERVICO},
    "relatorios-engie-atualizar.timer": {"titulo": "Atualização a cada 10 minutos", "tipo": TIMER},
    "relatorios-engie-atualizar.service": {"titulo": "Atualização do banco", "tipo": AVULSO},
    "relatorios-engie-parciais.timer": {"titulo": "PDF parcial todo dia às 05:30", "tipo": TIMER},
    "relatorios-engie-parciais.service": {"titulo": "Geração dos PDFs parciais", "tipo": AVULSO},
    "relatorios-engie-fechamento.timer": {"titulo": "Fechamento todo dia 1º às 06:00", "tipo": TIMER},
    "relatorios-engie-fechamento.service": {"titulo": "Fechamento do mês anterior", "tipo": AVULSO},
    "relatorios-engie-admin.service": {"titulo": "Este painel", "tipo": PROPRIO},
}
ACOES_UNIDADE = {SERVICO: {"start", "stop", "restart"}, TIMER: {"start", "stop", "enable", "disable"},
                 AVULSO: {"start"}, PROPRIO: set()}


class Erro(Exception):
    def __init__(self, status, mensagem):
        super().__init__(mensagem)
        self.status, self.mensagem = status, mensagem


class Arquivo:
    def __init__(self, caminho, tipo, nome):
        self.caminho, self.tipo, self.nome = Path(caminho), tipo, nome


def agora_txt():
    return datetime.now(disp.TZ).strftime("%d/%m/%Y %H:%M:%S")


def fmt_ts(t):
    return datetime.fromtimestamp(t, disp.TZ).strftime("%d/%m/%Y %H:%M:%S") if t else None


def carimbo():
    return datetime.now(disp.TZ).strftime("%Y%m%d-%H%M%S")


def mes_atual():
    return datetime.now(disp.TZ).strftime("%Y-%m")


def mes_anterior():
    hoje = datetime.now(disp.TZ)
    ano, m = (hoje.year, hoje.month - 1) if hoje.month > 1 else (hoje.year - 1, 12)
    return f"{ano:04d}-{m:02d}"


# ------------------------------------------------------- arquivos ----

_lock_arquivos = threading.Lock()


def gravar_atomico(caminho, texto, privado=False):
    """Grava em arquivo temporário e troca de uma vez: uma queda no meio não corrompe o original."""
    tmp = caminho.with_name(caminho.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if privado else 0o644)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(texto)
    os.replace(tmp, caminho)
    if privado:
        os.chmod(caminho, 0o600)


def auditar(usuario, acao, ip=None, **detalhe):
    """Registro permanente do painel (a tabela auditoria do banco é apagada pelo 'limpar')."""
    linha = json.dumps({"em": agora_txt(), "usuario": usuario, "ip": ip, "acao": acao, "detalhe": detalhe},
                       ensure_ascii=False)
    with _lock_arquivos:
        fd = os.open(LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(linha + "\n")


# ------------------------------------------------------------ env ----

def ler_env():
    out = {}
    if not ENV_FILE.exists():
        return out
    for linha in ENV_FILE.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$", linha)
        if m and not linha.lstrip().startswith("#"):
            v = m.group(2).strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            out[m.group(1)] = v
    return out


def ambiente():
    """Ambiente das tarefas: relê o env a cada uso, então editar o env vale já na próxima tarefa."""
    env = dict(os.environ)
    if ENV_FILE.exists():
        for k in ENV_CAMPOS:
            env.pop(k, None)
        env.update(ler_env())
    env.update(PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    return env


def valor_env(valor):
    """O env é lido pelo systemd e também por '. ./env' no shell: aspas simples servem aos dois."""
    return valor if re.fullmatch(r"[A-Za-z0-9_./:@%+=,-]*", valor) else f"'{valor}'"


def validar_env(chave, valor):
    campo = ENV_CAMPOS[chave]
    if "'" in valor or re.search(r"[\x00-\x1f\x7f]", valor):
        raise Erro(400, f"{campo['rotulo']}: aspas simples e quebras de linha não são aceitas")
    if campo.get("url") and not re.fullmatch(r"https?://[^\s]+", valor):
        raise Erro(400, f"{campo['rotulo']}: informe uma URL com http:// ou https://")
    if campo.get("bool") and valor not in ("0", "1"):
        raise Erro(400, f"{campo['rotulo']}: use 1 ou 0")
    if campo.get("regex"):
        try:
            if re.compile(valor).groups < 1:
                raise Erro(400, f"{campo['rotulo']}: a regex precisa de um grupo com o número")
        except re.error as e:
            raise Erro(400, f"{campo['rotulo']}: regex inválida ({e})") from e


def alterar_env(valores, remover):
    desconhecidas = (set(valores) | set(remover)) - set(ENV_CAMPOS)
    if desconhecidas:
        raise Erro(400, "variável não permitida: " + ", ".join(sorted(desconhecidas)))
    for k, v in valores.items():
        if not isinstance(v, str):
            raise Erro(400, f"{k}: valor inválido")
        validar_env(k, v)
    with _lock_arquivos:
        linhas = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
        if ENV_FILE.exists():
            gravar_atomico(ENV_FILE.with_name("env.anterior"), "\n".join(linhas) + "\n", privado=True)
        saida, feitas = [], set()
        for linha in linhas:
            m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=", linha)
            k = m.group(1) if m and not linha.lstrip().startswith("#") else None
            if k in remover:
                continue
            if k in valores:
                saida.append(f"{k}={valor_env(valores[k])}")
                feitas.add(k)
            else:
                saida.append(linha)
        saida += [f"{k}={valor_env(v)}" for k, v in valores.items() if k not in feitas]
        gravar_atomico(ENV_FILE, "\n".join(saida) + "\n", privado=True)


# ------------------------------------------------------- usuários ----

def hash_senha(senha):
    sal = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", senha.encode(), sal, PBKDF2_ITER)
    return f"pbkdf2_sha256${PBKDF2_ITER}${base64.b64encode(sal).decode()}${base64.b64encode(h).decode()}"


def confere_senha(senha, guardado):
    _, it, sal, h = guardado.split("$")
    calc = hashlib.pbkdf2_hmac("sha256", senha.encode(), base64.b64decode(sal), int(it))
    return hmac.compare_digest(calc, base64.b64decode(h))


SENHA_FALSA = hash_senha(secrets.token_hex(16))  # usuário inexistente leva o mesmo tempo


def ler_usuarios():
    return json.loads(USUARIOS_FILE.read_text(encoding="utf-8")) if USUARIOS_FILE.exists() else {}


def gravar_usuarios(usuarios):
    gravar_atomico(USUARIOS_FILE, json.dumps(usuarios, ensure_ascii=False, indent=2) + "\n", privado=True)


def validar_nome_usuario(nome):
    if not isinstance(nome, str) or not re.fullmatch(r"[a-zA-Z0-9._-]{2,32}", nome):
        raise Erro(400, "nome de usuário: de 2 a 32 letras, números, ponto, hífen ou sublinhado")


def validar_senha(senha):
    if not isinstance(senha, str) or len(senha) < SENHA_MIN:
        raise Erro(400, f"a senha precisa ter pelo menos {SENHA_MIN} caracteres")


def definir_senha(nome, senha):
    validar_nome_usuario(nome)
    validar_senha(senha)
    with _lock_arquivos:
        us = ler_usuarios()
        novo = nome not in us
        us[nome] = {"hash": hash_senha(senha), "criado_em": us.get(nome, {}).get("criado_em") or agora_txt(),
                    "senha_em": agora_txt()}
        gravar_usuarios(us)
    return novo


_sessoes, _falhas, _lock_sessoes = {}, {}, threading.Lock()


def criar_sessao(nome):
    token = secrets.token_urlsafe(32)
    with _lock_sessoes:
        _sessoes[token] = {"usuario": nome, "expira": time.time() + SESSAO_SEG}
    return token


def usuario_da_sessao(token):
    with _lock_sessoes:
        s = _sessoes.get(token) if token else None
        if not s:
            return None
        if s["expira"] < time.time():
            del _sessoes[token]
            return None
        s["expira"] = time.time() + SESSAO_SEG
        return s["usuario"]


def encerrar_sessoes(nome, exceto=None):
    with _lock_sessoes:
        for t in [t for t, s in _sessoes.items() if s["usuario"] == nome and t != exceto]:
            del _sessoes[t]


def bloqueado(chave):
    with _lock_sessoes:
        f = _falhas.get(chave)
        return bool(f and f[0] >= TENTATIVAS_MAX and time.time() - f[1] < BLOQUEIO_SEG)


def registrar_falha(chave):
    with _lock_sessoes:
        n, ult = _falhas.get(chave, (0, 0))
        n = 1 if time.time() - ult >= BLOQUEIO_SEG else n + 1
        _falhas[chave] = (n, time.time())


# ------------------------------------------------- banco e validação ----

def caminho_db():
    return ambiente().get("DISP_DB") or str(BASE / "disponibilidade.db")


def pasta_pdf():
    return Path(ambiente().get("PDF_DIR") or BASE / "pdf")


@contextmanager
def banco():
    conn = disp.conectar(caminho_db())
    try:
        yield conn
    finally:
        conn.close()


def definicoes():
    return json.loads(DEFS.read_text(encoding="utf-8"))


def relatorios_medidos(conn):
    return [r[0] for r in conn.execute("SELECT DISTINCT relatorio FROM objetos ORDER BY 1")]


def texto(d, chave, rotulo, maximo=500, obrigatorio=True):
    v = d.get(chave)
    v = v.strip() if isinstance(v, str) else ""
    if not v and obrigatorio:
        raise Erro(400, f"informe {rotulo}")
    if len(v) > maximo:
        raise Erro(400, f"{rotulo}: no máximo {maximo} caracteres")
    if re.search(r"[\x00-\x08\x0b-\x1f\x7f]", v):
        raise Erro(400, f"{rotulo}: caracteres inválidos")
    return v


def val_mes(v):
    if not isinstance(v, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", v):
        raise Erro(400, "mês inválido: use AAAA-MM")
    return v


def val_data(v, rotulo="data"):
    try:
        datetime.strptime(v, "%Y-%m-%d")
    except (TypeError, ValueError) as e:
        raise Erro(400, f"{rotulo} inválida: use AAAA-MM-DD") from e
    return v


def val_datahora(v, rotulo="data e hora"):
    """Aceita 'AAAA-MM-DDTHH:MM' (campo do navegador). Devolve o formato do disp.py."""
    if isinstance(v, str):
        v = v.replace("T", " ")[:16]
        try:
            datetime.strptime(v, "%Y-%m-%d %H:%M")
            return v
        except ValueError:
            pass
    raise Erro(400, f"{rotulo} inválida")


def val_intervalo(d):
    ini = val_datahora(d.get("inicio"), "início")
    fim = val_datahora(d.get("fim"), "fim")
    if disp.epoch(fim) <= disp.epoch(ini):
        raise Erro(400, "o fim precisa ser depois do início")
    return ini, fim


def val_inteiro(v, rotulo, minimo, maximo):
    try:
        n = int(v)
    except (TypeError, ValueError) as e:
        raise Erro(400, f"{rotulo}: informe um número") from e
    if not minimo <= n <= maximo:
        raise Erro(400, f"{rotulo}: de {minimo} a {maximo}")
    return n


def val_chamado(v):
    if not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,39}", v):
        raise Erro(400, "número de chamado inválido")
    return v


def val_tipo_expurgo(v):
    if v not in disp.TIPOS_EXPURGO:
        raise Erro(400, "tipo de expurgo inválido")
    return v


def val_pdf_id(v):
    if v not in definicoes():
        raise Erro(400, "relatório não definido")
    return v


def periodo_consulta(q):
    """Devolve a chave de período do motor: 'AAAA-MM' ou 'AAAA-MM-DD..AAAA-MM-DD'."""
    if q.get("de") or q.get("ate"):
        de, ate = val_data(q.get("de"), "data inicial"), val_data(q.get("ate"), "data final")
        if ate < de:
            raise Erro(400, "a data final é anterior à inicial")
        return f"{de}..{ate}"
    return val_mes(q.get("mes") or mes_atual())


def menos_meses(d, n):
    total = d.year * 12 + d.month - 1 - n
    ano, mes = total // 12, total % 12 + 1
    return date(ano, mes, min(d.day, calendar.monthrange(ano, mes)[1]))


def periodos_prontos(hoje=None):
    """Períodos oferecidos na tela de gerar relatório, no horário de Brasília."""
    hoje = hoje or datetime.now(disp.TZ).date()
    livre = lambda pid, rotulo, de, ate=hoje: {"id": pid, "rotulo": rotulo, "de": de.isoformat(), "ate": ate.isoformat()}
    anterior = menos_meses(hoje.replace(day=1), 1)
    fim_semana = hoje - timedelta(days=hoje.isoweekday())       # domingo passado
    return [
        livre("7d", "Últimos 7 dias", hoje - timedelta(days=6)),
        livre("15d", "Últimos 15 dias", hoje - timedelta(days=14)),
        livre("30d", "Últimos 30 dias", hoje - timedelta(days=29)),
        livre("semana", "Semana passada", fim_semana - timedelta(days=6), fim_semana),
        {"id": "mes", "rotulo": "Este mês", "mes": hoje.strftime("%Y-%m"), "de": hoje.replace(day=1).isoformat(), "ate": hoje.isoformat()},
        {"id": "mes-anterior", "rotulo": "Mês passado", "mes": anterior.strftime("%Y-%m"), "de": anterior.isoformat(),
         "ate": (hoje.replace(day=1) - timedelta(days=1)).isoformat()},
        livre("3m", "Últimos 3 meses", menos_meses(hoje, 3)),
    ]


def executar(*args, tempo=180):
    """Roda um comando do projeto (sem shell) e devolve a saída; falha vira erro legível."""
    try:
        r = subprocess.run([sys.executable, *map(str, args)], cwd=BASE, env=ambiente(), capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=tempo)
    except subprocess.TimeoutExpired as e:
        raise Erro(504, "o comando demorou demais") from e
    if r.returncode != 0:
        linhas = [l for l in (r.stderr or r.stdout).splitlines() if l.strip()]
        raise Erro(400, linhas[-1] if linhas else f"comando falhou (código {r.returncode})")
    return r.stdout.strip()


def meses_fechados(conn, inicio, fim):
    """Meses do intervalo que já têm fechamento: o PDF oficial deles não muda sem nova versão."""
    a, b = datetime.fromtimestamp(inicio, disp.TZ), datetime.fromtimestamp(fim, disp.TZ)
    meses, ano, m = [], a.year, a.month
    while (ano, m) <= (b.year, b.month):
        meses.append(f"{ano:04d}-{m:02d}")
        ano, m = (ano + 1, 1) if m == 12 else (ano, m + 1)
    ph = ",".join("?" * len(meses))
    return [r[0] for r in conn.execute(f"SELECT DISTINCT mes FROM fechamentos WHERE mes IN ({ph}) ORDER BY 1", meses)]


def aviso_fechados(meses):
    if not meses:
        return None
    return (f"{', '.join(meses)} já tem fechamento: o PDF oficial só muda se você gerar uma nova versão "
            "em Relatórios > Fechamento.")


# ------------------------------------------------------- systemd ----

def rodar_sistema(argv, tempo=20):
    try:
        r = subprocess.run([shutil.which(argv[0]) or argv[0], *argv[1:]], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=tempo)
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


def estado_unidades():
    if not shutil.which("systemctl"):
        return {"disponivel": False, "unidades": []}
    props = "LoadState,ActiveState,SubState,UnitFileState,Result,NextElapseUSecRealtime,LastTriggerUSec,ExecMainExitTimestamp"
    out = []
    for nome, u in UNIDADES.items():
        _, saida, _ = rodar_sistema(["systemctl", "show", nome, "--no-pager", "-p", props])
        p = dict(l.split("=", 1) for l in saida.splitlines() if "=" in l)
        out.append({"nome": nome, "titulo": u["titulo"], "tipo": u["tipo"], "acoes": sorted(ACOES_UNIDADE[u["tipo"]]),
                    "carregada": p.get("LoadState"), "ativa": p.get("ActiveState"), "sub": p.get("SubState"),
                    "habilitada": p.get("UnitFileState"), "resultado": p.get("Result"),
                    "proxima": p.get("NextElapseUSecRealtime") or None, "ultima": p.get("LastTriggerUSec") or None,
                    "terminou": p.get("ExecMainExitTimestamp") or None})
    return {"disponivel": True, "unidades": out}


# -------------------------------------------------------- tarefas ----

_tarefas, _lock_tarefas = collections.OrderedDict(), threading.Lock()
_contador = {"n": 0}


def tarefa_publica(t, desde=None):
    d = {k: t[k] for k in ("id", "tipo", "titulo", "por", "inicio", "fim", "estado", "codigo")}
    if desde is not None:
        d["saida"], d["total_linhas"] = t["saida"][desde:], len(t["saida"])
    return d


def _rodar_tarefa(t, argv):
    try:
        t["proc"] = p = subprocess.Popen(argv, cwd=BASE, env=ambiente(), stdin=subprocess.DEVNULL,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                         encoding="utf-8", errors="replace")
        limite = threading.Timer(TAREFA_MAX_SEG, p.kill)
        limite.daemon = True
        limite.start()
        for linha in p.stdout:
            if len(t["saida"]) < TAREFA_MAX_LINHAS:
                t["saida"].append(linha.rstrip("\n"))
        t["codigo"] = p.wait()
        limite.cancel()
        t["estado"] = "cancelada" if t["cancelada"] else ("concluida" if t["codigo"] == 0 else "falhou")
    except Exception as e:  # noqa: BLE001
        t["saida"].append(f"[!] não foi possível executar: {e}")
        t["estado"] = "falhou"
    t["fim"] = agora_txt()
    auditar(t["por"], "tarefa-fim", tarefa=t["tipo"], titulo=t["titulo"], estado=t["estado"], codigo=t["codigo"])


def iniciar_tarefa(tipo, titulo, argv, por, ip):
    with _lock_tarefas:
        if any(t["tipo"] == tipo and t["estado"] == "executando" for t in _tarefas.values()):
            raise Erro(409, "esta tarefa já está em execução")
        _contador["n"] += 1
        t = {"id": _contador["n"], "tipo": tipo, "titulo": titulo, "por": por, "inicio": agora_txt(), "fim": None,
             "estado": "executando", "codigo": None, "saida": [], "proc": None, "cancelada": False}
        _tarefas[t["id"]] = t
        antigas = [i for i, x in _tarefas.items() if x["estado"] != "executando"]
        for i in antigas[:max(0, len(_tarefas) - TAREFAS_GUARDADAS)]:
            del _tarefas[i]
    auditar(por, "tarefa-inicio", ip, tarefa=tipo, titulo=titulo, comando=[str(a) for a in argv[1:]])
    threading.Thread(target=_rodar_tarefa, args=(t, argv), daemon=True).start()
    return t


def montar_tarefa(tipo, p, usuario):
    """Catálogo fechado: nada do navegador vira comando sem passar por um validador daqui."""
    py = sys.executable
    if tipo == "atualizar":
        if not (BASE / "atualizar.sh").exists():
            raise Erro(400, "atualizar.sh não existe nesta pasta (passo 4 da instalação)")
        return "Atualizar tudo agora", ["bash", str(BASE / "atualizar.sh")]
    if tipo == "sync-objetos":
        desde = p.get("desde")
        return ("Sincronizar itens do Zabbix",
                [py, "disp.py", "sync-objetos"] + (["--desde", val_data(desde, "início da medição")] if desde else []))
    if tipo == "importar-eventos":
        de, ate = val_data(p.get("de"), "data inicial"), val_data(p.get("ate"), "data final")
        if ate < de:
            raise Erro(400, "a data final é anterior à inicial")
        return f"Importar quedas de {de} a {ate}", [py, "disp.py", "importar-eventos", "--de", de, "--ate", ate]
    if tipo == "importar-chamados":
        if p.get("chamado"):
            n = val_chamado(p["chamado"].strip())
            return f"Importar chamado {n}", [py, "disp.py", "importar-chamados", f"--chamado={n}"]
        dias = val_inteiro(p.get("dias") or 45, "dias", 1, 365)
        return f"Importar chamados dos últimos {dias} dias", [py, "disp.py", "importar-chamados", "--dias", str(dias)]
    if tipo == "parciais":
        mes = val_mes(p.get("mes") or mes_atual())
        return f"Gerar PDFs parciais de {mes}", [py, "relatorio.py", "parciais", "--mes", mes]
    if tipo == "gerar":
        periodo = periodo_consulta(p)
        destino = pasta_pdf() / "avulsos" / carimbo()
        destino.mkdir(parents=True, exist_ok=True)
        tempo = ["--de", p["de"], "--ate", p["ate"]] if ".." in periodo else ["--mes", periodo]
        rid = p.get("relatorio")
        if not rid:
            return f"Gerar todos os PDFs de {periodo}", [py, "relatorio.py", "gerar", "--todos", *tempo, f"--saida={destino}"]
        val_pdf_id(rid)
        return (f"Gerar PDF {rid} de {periodo}",
                [py, "relatorio.py", "gerar", f"--relatorio={rid}", *tempo,
                 f"--saida={destino / (rid + ' ' + periodo.replace('..', ' a ') + '.pdf')}"])
    if tipo == "fechar":
        rid, mes = val_pdf_id(p.get("relatorio")), val_mes(p.get("mes"))
        nova = bool(p.get("nova_versao"))
        return (f"{'Nova versão do fechamento' if nova else 'Fechar'} {rid} {mes}",
                [py, "relatorio.py", "fechar", f"--relatorio={rid}", "--mes", mes, f"--por={usuario}"]
                + (["--nova-versao"] if nova else []))
    if tipo == "fechar-mes-anterior":
        return f"Fechar todos os relatórios de {mes_anterior()}", [py, "relatorio.py", "fechar-mes-anterior"]
    if tipo == "limpar":
        meses = val_inteiro(p.get("meses"), "meses", 1, 120)
        return f"Limpar dados, mantendo {meses} meses", [py, "disp.py", "limpar", "--meses", str(meses)]
    if tipo == "criar-triggers":
        aplicar = bool(p.get("aplicar"))
        return (("Criar triggers TID no Zabbix" if aplicar else "Simular criação de triggers TID"),
                [py, "criar_triggers_tid.py"] + (["--aplicar"] if aplicar else []))
    if tipo == "testes":
        return "Testes do cálculo", [py, "-m", "pytest", "-q", "testes/test_disp.py"]
    if tipo in ("testar-zabbix", "testar-topdesk"):
        alvo = tipo.split("-")[1]
        return f"Testar conexão com {alvo}", [py, "admin.py", "testar", alvo]
    if tipo == "carga-inicial":
        desde = val_data(p.get("desde"), "início da medição")
        return f"Carga inicial desde {desde}", [py, "admin.py", "carga-inicial", desde]
    raise Erro(400, "tarefa desconhecida")


# ---------------------------------------------------- definições ----

TIPOS_SECAO = {"indicadores", "tabela", "grupos", "evidencias", "chamados"}


def validar_definicao(d):
    """Pega antes de gravar o que quebraria o PDF só na hora de gerar (ou no fechamento do dia 1º)."""
    if not isinstance(d, dict):
        return ["a definição precisa ser um objeto JSON"]
    erros = []
    for k in ("titulo", "arquivo"):
        if not isinstance(d.get(k), str) or not d[k].strip():
            erros.append(f"campo obrigatório: {k}")
    arq = d.get("arquivo")
    if isinstance(arq, str):
        if re.search(r"[/\\]|\.\.", arq):
            erros.append("arquivo: não pode ter barras nem '..'")
        try:
            chaves = {c for _, c, _, _ in string.Formatter().parse(arq) if c is not None}
            if chaves - {"mes_nome", "ano", "mes"}:
                erros.append("arquivo: use só {mes_nome}, {ano} e {mes}")
        except ValueError:
            erros.append("arquivo: chaves { } malformadas")
    meta = d.get("meta", 99.9)
    if isinstance(meta, bool) or not isinstance(meta, (int, float)) or not 0 <= meta <= 100:
        erros.append("meta: número de 0 a 100")
    secoes = d.get("secoes")
    if not isinstance(secoes, list) or not secoes:
        return erros + ["secoes: informe ao menos uma seção"]
    for n, s in enumerate(secoes, 1):
        rot = f"seção {n}"
        if not isinstance(s, dict):
            erros.append(f"{rot}: precisa ser um objeto")
            continue
        if s.get("tipo") not in TIPOS_SECAO:
            erros.append(f"{rot}: tipo deve ser um de {', '.join(sorted(TIPOS_SECAO))}")
            continue
        if not isinstance(s.get("titulo"), str) or not s["titulo"].strip():
            erros.append(f"{rot}: falta o título")
        if s.get("formato") not in (None, "", "powerplant", "apos_on"):
            erros.append(f"{rot}: formato deve ser powerplant ou apos_on")
        if "limite" in s and (isinstance(s["limite"], bool) or not isinstance(s["limite"], int) or s["limite"] < 1):
            erros.append(f"{rot}: limite deve ser inteiro positivo")
        if s["tipo"] == "indicadores":
            inds = s.get("indicadores")
            if not isinstance(inds, list) or not inds:
                erros.append(f"{rot}: informe a lista de indicadores")
                continue
            for i in inds:
                if not isinstance(i, dict) or not i.get("rotulo") or not i.get("chave"):
                    erros.append(f"{rot}: cada indicador precisa de rotulo e chave")
                elif not i.get("relatorio") and not s.get("relatorios"):
                    erros.append(f"{rot}: indicador '{i['rotulo']}' sem relatorio (nem 'relatorios' na seção)")
        elif not isinstance(s.get("relatorio"), str) or not s["relatorio"]:
            erros.append(f"{rot}: falta o campo relatorio (id da medição)")
    return erros


def medicoes_da_definicao(d):
    ids = set()
    for s in d.get("secoes", []):
        ids.update(s.get("relatorios", []))
        if s.get("relatorio"):
            ids.add(s["relatorio"])
        ids.update(i["relatorio"] for i in s.get("indicadores", []) if i.get("relatorio"))
    return ids


def gravar_definicoes(defs):
    with _lock_arquivos:
        BACKUPS.mkdir(exist_ok=True)
        if DEFS.exists():
            shutil.copy2(DEFS, BACKUPS / f"relatorios-{carimbo()}.json")
        gravar_atomico(DEFS, json.dumps(defs, ensure_ascii=False, indent=2) + "\n")


# ---------------------------------------------------------- rotas ----

ROTAS = []


def rota(metodo, padrao):
    def deco(f):
        ROTAS.append((metodo, re.compile(f"^/api{padrao}$"), f))
        return f
    return deco


class Ctx:
    def __init__(self, q, corpo, usuario, ip, token):
        self.q, self.b, self.usuario, self.ip, self.token = q, corpo, usuario, ip, token

    def registrar(self, acao, **detalhe):
        auditar(self.usuario, acao, self.ip, **detalhe)


@rota("GET", "/listas")
def r_listas(c):
    with banco() as conn:
        med = relatorios_medidos(conn)
    defs = definicoes()
    return {"relatorios_pdf": [{"id": k, "titulo": v.get("titulo", k)} for k, v in defs.items()], "medicoes": med,
            "tipos_expurgo": disp.TIPOS_EXPURGO, "mes_atual": mes_atual(), "mes_anterior": mes_anterior(),
            "usuario": c.usuario,
            "links": {"Grafana": ler_env()["GRAFANA_URL"]} if ler_env().get("GRAFANA_URL") else {}}


@rota("GET", "/periodos")
def r_periodos(c):
    hoje = datetime.now(disp.TZ).date()
    return {"periodos": periodos_prontos(hoje), "hoje": hoje.isoformat(), "meses_guardados": MESES_GUARDADOS,
            "dados_desde": menos_meses(hoje.replace(day=1), MESES_GUARDADOS).isoformat()}


@rota("GET", "/painel")
def r_painel(c):
    alertas = []
    servicos = estado_unidades()
    for u in servicos["unidades"]:
        if u["tipo"] == PROPRIO:
            continue
        if u["carregada"] != "loaded":
            alertas.append({"nivel": "erro", "texto": f"{u['titulo']}: unidade não instalada", "ir": "servicos"})
        elif u["ativa"] == "failed" or (u["tipo"] == AVULSO and u["resultado"] not in ("success", None)):
            alertas.append({"nivel": "erro", "texto": f"{u['titulo']}: a última execução falhou", "ir": "servicos"})
        elif u["tipo"] in (SERVICO, TIMER) and u["ativa"] != "active":
            alertas.append({"nivel": "erro", "texto": f"{u['titulo']}: parado", "ir": "servicos"})
    mes = mes_atual()
    m_ini, m_fim = disp.limites_mes(mes)
    agora = int(time.time())
    with banco() as conn:
        medicoes = []
        for r in conn.execute("SELECT relatorio, count(*) n FROM objetos WHERE ativo_ate IS NULL GROUP BY 1 ORDER BY 1").fetchall():
            q = conn.execute("SELECT count(*) n, sum(i.fim IS NULL) abertas FROM incidentes i JOIN objetos o ON o.id=i.objeto_id "
                             "WHERE o.relatorio=? AND i.inicio<? AND coalesce(i.fim,?)>?",
                             (r["relatorio"], m_fim, agora, m_ini)).fetchone()
            medicoes.append({"relatorio": r["relatorio"], "itens": r["n"], "quedas": q["n"], "abertas": q["abertas"] or 0})
        propostos = conn.execute("SELECT count(*) FROM expurgos WHERE status='proposto'").fetchone()[0]
        macros = conn.execute("SELECT count(*) FROM objetos WHERE ativo_ate IS NULL AND "
                              "(item LIKE '%{$%' OR coalesce(grupo,'') LIKE '%{$%')").fetchone()[0]
        sem_chamado = conn.execute(
            "SELECT count(DISTINCT i.chamado) FROM incidentes i LEFT JOIN chamados c ON c.numero=i.chamado "
            "WHERE i.chamado IS NOT NULL AND c.numero IS NULL AND i.inicio>=?", (agora - 45 * 86400,)).fetchone()[0]
        anterior = mes_anterior()
        fim_ant = disp.limites_mes(anterior)[1]
        fechados = {r[0] for r in conn.execute("SELECT DISTINCT relatorio FROM fechamentos WHERE mes=?", (anterior,))}
        tem_dados = conn.execute("SELECT 1 FROM objetos WHERE ativo_desde<? LIMIT 1", (fim_ant,)).fetchone()
        ultimo = conn.execute("SELECT max(atualizado_em) FROM incidentes").fetchone()[0]
    faltam = [k for k in definicoes() if k not in fechados]
    if tem_dados and faltam:
        alertas.append({"nivel": "aviso", "texto": f"{len(faltam)} relatório(s) de {anterior} sem fechamento", "ir": "fechamento"})
    if propostos:
        alertas.append({"nivel": "aviso", "texto": f"{propostos} expurgo(s) aguardando decisão", "ir": "expurgos"})
    if macros:
        alertas.append({"nivel": "aviso", "texto": f"{macros} item(ns) com macro sem valor no Zabbix", "ir": "itens"})
    if sem_chamado:
        alertas.append({"nivel": "info", "texto": f"{sem_chamado} chamado(s) ainda não consultados no TOPdesk", "ir": "tarefas"})
    db = Path(caminho_db())
    disco = shutil.disk_usage(db.parent if db.parent.exists() else BASE)
    return {"alertas": alertas, "medicoes": medicoes, "mes": mes, "systemd": servicos["disponivel"],
            "banco": {"caminho": str(db), "tamanho": db.stat().st_size if db.exists() else 0,
                      "disco_livre": disco.free, "disco_total": disco.total, "ultima_alteracao": fmt_ts(ultimo)}}


@rota("GET", "/quedas")
def r_quedas(c):
    q = {"relatorio": texto(c.q, "relatorio", "a medição", 100), "mes": periodo_consulta(c.q), "ordem": "desc"}
    if c.q.get("grupo"):
        q["grupo"] = c.q["grupo"]
    with banco() as conn:
        linhas = api.incidentes(conn, q)
        titulos = dict(conn.execute("SELECT numero, titulo FROM chamados").fetchall())
    if c.q.get("item"):                     # vem do seletor: é o item exato, não parte do nome
        linhas = [l for l in linhas if l["ITEM"] == c.q["item"]]
    for l in linhas:
        l["CHAMADO_TITULO"] = titulos.get(l["CHAMADO"])
    if c.q.get("situacao") == "com-chamado":
        linhas = [l for l in linhas if l["CHAMADO"]]
    elif c.q.get("situacao") == "abertas":
        linhas = [l for l in linhas if not l["FIM"]]
    elif c.q.get("situacao") == "desconsideradas":
        linhas = [l for l in linhas if l["EXPURGO"]]
    elif c.q.get("situacao") == "contabilizadas":
        linhas = [l for l in linhas if l["EXPURGO"] != "total"]
    return linhas


@rota("GET", "/quedas/itens")
def r_quedas_itens(c):
    """Opções do seletor: itens da medição e quantas quedas cada um teve no período."""
    rel = texto(c.q, "relatorio", "a medição", 100)
    ini, fim = disp.limites_mes(periodo_consulta(c.q))
    agora = int(time.time())
    with banco() as conn:
        return [{"item": r["item"], "quedas": r["n"]} for r in conn.execute(
            "SELECT o.item, count(i.id) n, o.ativo_ate FROM objetos o LEFT JOIN incidentes i ON i.objeto_id=o.id "
            "AND i.inicio<? AND coalesce(i.fim,?)>? WHERE o.relatorio=? GROUP BY o.id "
            "HAVING o.ativo_ate IS NULL OR n>0 ORDER BY o.item", (fim, agora, ini, rel))]


@rota("GET", r"/quedas/(\d+)")
def r_queda(c, iid):
    iid = int(iid)
    with banco() as conn:
        i = conn.execute("SELECT i.*, o.relatorio, o.item, o.grupo FROM incidentes i JOIN objetos o ON o.id=i.objeto_id "
                         "WHERE i.id=?", (iid,)).fetchone()
        if not i:
            raise Erro(404, "queda não encontrada")
        comentarios = [{"em": fmt_ts(r["clock"]), "usuario": r["usuario"], "mensagem": r["mensagem"]} for r in conn.execute(
            "SELECT clock, usuario, mensagem FROM comentarios WHERE incidente_id=? ORDER BY clock", (iid,))]
        chamado = None
        if i["chamado"]:
            ch = conn.execute("SELECT * FROM chamados WHERE numero=?", (i["chamado"],)).fetchone()
            if ch:
                chamado = {"numero": ch["numero"], "titulo": ch["titulo"], "status": ch["status"],
                           "encerrado": bool(ch["encerrado"]), "aberto_em": fmt_ts(ch["aberto_em"]),
                           "encerrado_em": fmt_ts(ch["encerrado_em"]), "lido_em": fmt_ts(ch["atualizado_em"]),
                           "acoes": [{"em": fmt_ts(a["clock"]), "autor": a["autor"], "analista": bool(a["analista"]),
                                      "texto": a["texto"]} for a in conn.execute(
                               "SELECT * FROM chamado_acoes WHERE chamado=? ORDER BY clock", (i["chamado"],))]}
        expurgos = [expurgo_publico(r) for r in conn.execute(
            "SELECT e.*, coalesce(o.relatorio,e.relatorio) AS rel, coalesce(o.item,'(medição inteira)') AS it "
            "FROM expurgos e LEFT JOIN objetos o ON o.id=e.objeto_id WHERE e.incidente_id=? ORDER BY e.id", (iid,))]
    return {"id": iid, "relatorio": i["relatorio"], "item": i["item"], "grupo": i["grupo"], "problema": i["problema"],
            "inicio": fmt_ts(i["inicio"]), "fim": fmt_ts(i["fim"]), "chamado_numero": i["chamado"],
            "chamado_externo": i["chamado_externo"], "causa": i["causa"], "evento": i["zabbix_eventid"],
            "comentarios": comentarios, "chamado": chamado, "expurgos": expurgos}


def norma_chamado(texto):
    """'i 2611-123' e 'I2611 123' são o mesmo chamado: o número pode ter sido gravado com espaços."""
    return re.sub(r"[\s-]", "", texto).upper()


def separar_chamados(texto, conhecidos):
    """Devolve os chamados da lista, na ordem e sem repetir. Vírgula, ponto e vírgula e linha sempre separam;
    espaço só separa quando o trecho inteiro não é um chamado conhecido (o número pode conter espaços)."""
    pedidos = []
    for trecho in re.split(r"[\r\n,;]+", texto):
        trecho = trecho.strip()
        if trecho:
            pedidos += [trecho] if norma_chamado(trecho) in conhecidos else trecho.split()
    return list({norma_chamado(p): p for p in reversed(pedidos)}.values())[::-1]


@rota("POST", "/quedas/conferir-chamados")
def r_conferir_chamados(c):
    """Só consulta: mostra o que a lista alcança antes de qualquer alteração."""
    texto_lista = c.b.get("texto")
    if not isinstance(texto_lista, str) or not texto_lista.strip():
        raise Erro(400, "informe os números dos chamados")
    if len(texto_lista) > 20000:
        raise Erro(400, "lista grande demais")
    with banco() as conn:
        conhecidos = {}
        for r in conn.execute("SELECT DISTINCT chamado FROM incidentes WHERE chamado IS NOT NULL"):
            conhecidos.setdefault(norma_chamado(r[0]), []).append(r[0])
        pedidos = separar_chamados(texto_lista, conhecidos)
        if len(pedidos) > 200:
            raise Erro(400, "no máximo 200 chamados de cada vez")
        faltam = [p for p in pedidos if norma_chamado(p) not in conhecidos]
        numeros = [n for p in pedidos for n in conhecidos.get(norma_chamado(p), [])]
        quedas = []
        if numeros:
            ph = ",".join("?" * len(numeros))
            agora = int(time.time())
            for r in conn.execute(
                    "SELECT i.id, i.chamado, i.inicio, i.fim, o.relatorio, o.item FROM incidentes i "
                    f"JOIN objetos o ON o.id=i.objeto_id WHERE i.chamado IN ({ph}) ORDER BY i.inicio DESC, o.relatorio", numeros):
                situacao, _ = disp.situacao_expurgo(conn, r["id"])
                quedas.append({"id": r["id"], "chamado": r["chamado"], "relatorio": r["relatorio"], "item": r["item"],
                               "inicio": fmt_ts(r["inicio"]), "fim": fmt_ts(r["fim"]),
                               "minutos": ((r["fim"] or agora) - r["inicio"]) // 60, "expurgo": situacao})
    return {"pedidos": len(pedidos), "nao_encontrados": faltam, "quedas": quedas}


@rota("POST", "/quedas/desconsiderar")
def r_desconsiderar(c):
    ids = c.b.get("incidentes")
    if not isinstance(ids, list) or not ids or len(ids) > 500 or not all(isinstance(i, int) for i in ids):
        raise Erro(400, "selecione as quedas")
    tipo, motivo = val_tipo_expurgo(c.b.get("tipo")), texto(c.b, "motivo", "o motivo")
    argv = ["disp.py", "desconsiderar", f"--tipo={tipo}", f"--motivo={motivo}", f"--por={c.usuario}"]
    for i in ids:
        argv.append(f"--incidente={i}")
    if c.b.get("inicio") or c.b.get("fim"):
        if len(ids) != 1:
            raise Erro(400, "o trecho só vale para uma queda de cada vez")
        ini, fim = val_intervalo(c.b)
        argv += [f"--inicio={ini}", f"--fim={fim}"]
    saida = executar(*argv)
    c.registrar("desconsiderar", incidentes=ids, tipo=tipo, motivo=motivo, inicio=c.b.get("inicio"), fim=c.b.get("fim"))
    with banco() as conn:
        ph = ",".join("?" * len(ids))
        r = conn.execute(f"SELECT min(inicio) a, max(coalesce(fim, ?)) b FROM incidentes WHERE id IN ({ph})",
                         [int(time.time()), *ids]).fetchone()
        aviso = aviso_fechados(meses_fechados(conn, r["a"], r["b"])) if r["a"] else None
    return {"mensagem": saida.splitlines()[-1] if saida else "feito", "aviso": aviso}


@rota("POST", "/quedas/contabilizar")
def r_contabilizar(c):
    ids = c.b.get("incidentes")
    if not isinstance(ids, list) or not ids or len(ids) > 500 or not all(isinstance(i, int) for i in ids):
        raise Erro(400, "selecione as quedas")
    with banco() as conn:
        ph = ",".join("?" * len(ids))
        expurgos = [r[0] for r in conn.execute(
            f"SELECT id FROM expurgos WHERE status='aprovado' AND incidente_id IN ({ph})", ids)]
        r = conn.execute(f"SELECT min(inicio) a, max(coalesce(fim, ?)) b FROM incidentes WHERE id IN ({ph})",
                         [int(time.time()), *ids]).fetchone()
        aviso = aviso_fechados(meses_fechados(conn, r["a"], r["b"])) if r["a"] else None
    if not expurgos:
        raise Erro(400, "estas quedas estão cobertas por um expurgo de janela: desfaça na aba Expurgos")
    for e in expurgos:
        executar("disp.py", "contabilizar", f"--id={e}", f"--por={c.usuario}")
    c.registrar("contabilizar", incidentes=ids, expurgos=expurgos)
    return {"mensagem": f"{len(expurgos)} expurgo(s) desfeito(s): as quedas voltam a contar", "aviso": aviso}


def expurgo_publico(e):
    return {"id": e["id"], "status": e["status"], "relatorio": e["rel"], "item": e["it"], "inicio": fmt_ts(e["inicio"]),
            "fim": fmt_ts(e["fim"]), "tipo": e["tipo"], "tipo_nome": disp.TIPOS_EXPURGO.get(e["tipo"], e["tipo"]),
            "motivo": e["motivo"], "chamado": e["chamado"], "proposto_por": e["proposto_por"],
            "proposto_em": fmt_ts(e["proposto_em"]), "decidido_por": e["decidido_por"], "decidido_em": fmt_ts(e["decidido_em"])}


@rota("GET", "/expurgos")
def r_expurgos(c):
    sql = ("SELECT e.*, coalesce(o.relatorio,e.relatorio) AS rel, coalesce(o.item,'(medição inteira)') AS it "
           "FROM expurgos e LEFT JOIN objetos o ON o.id=e.objeto_id WHERE 1=1")
    args = []
    if c.q.get("relatorio"):
        sql += " AND coalesce(o.relatorio,e.relatorio)=?"
        args.append(c.q["relatorio"])
    if c.q.get("mes"):
        ini, fim = disp.limites_mes(val_mes(c.q["mes"]))
        sql += " AND e.inicio<? AND e.fim>?"
        args += [fim, ini]
    if c.q.get("status"):
        sql += " AND e.status=?"
        args.append(c.q["status"])
    with banco() as conn:
        return [expurgo_publico(r) for r in conn.execute(sql + " ORDER BY e.inicio DESC, e.id DESC LIMIT 1000", args)]


@rota("POST", "/expurgos")
def r_expurgo_novo(c):
    ini, fim = val_intervalo(c.b)
    tipo, motivo = val_tipo_expurgo(c.b.get("tipo")), texto(c.b, "motivo", "o motivo")
    relatorio = texto(c.b, "relatorio", "a medição", 100)
    with banco() as conn:
        if relatorio not in relatorios_medidos(conn):
            raise Erro(400, "medição desconhecida")
        alvo = [f"--relatorio={relatorio}"]
        if c.b.get("objeto"):
            o = conn.execute("SELECT id FROM objetos WHERE id=? AND relatorio=?", (c.b["objeto"], relatorio)).fetchone()
            if not o:
                raise Erro(400, "item não pertence a esta medição")
            alvo = [f"--objeto={o['id']}"]
        aviso = aviso_fechados(meses_fechados(conn, disp.epoch(ini), disp.epoch(fim)))
    chamado = (c.b.get("chamado") or "").strip()
    argv = ["disp.py", "expurgo-propor", *alvo, f"--inicio={ini}", f"--fim={fim}", f"--tipo={tipo}",
            f"--motivo={motivo}", f"--por={c.usuario}"]
    if chamado:
        argv.append(f"--chamado={val_chamado(chamado)}")
    r = json.loads(executar(*argv))
    c.registrar("expurgo-novo", id=r["expurgo_id"], relatorio=relatorio, objeto=c.b.get("objeto"), inicio=ini, fim=fim,
                tipo=tipo, motivo=motivo)
    return {"mensagem": f"Expurgo #{r['expurgo_id']} proposto: falta aprovar para valer no cálculo", "aviso": aviso}


@rota("POST", r"/expurgos/(\d+)/(aprovar|rejeitar|revogar)")
def r_expurgo_decidir(c, eid, acao):
    if acao == "revogar":
        executar("disp.py", "expurgo-revogar", "--id", eid, f"--por={c.usuario}")
    else:
        executar("disp.py", "expurgo-decidir", "--id", eid,
                 "--status", "aprovado" if acao == "aprovar" else "rejeitado", f"--por={c.usuario}")
    c.registrar(f"expurgo-{acao}", id=int(eid))
    with banco() as conn:
        e = conn.execute("SELECT inicio, fim FROM expurgos WHERE id=?", (int(eid),)).fetchone()
        aviso = aviso_fechados(meses_fechados(conn, e["inicio"], e["fim"])) if e else None
    return {"mensagem": f"Expurgo #{eid}: {acao} feito", "aviso": aviso}


@rota("GET", "/lacunas")
def r_lacunas(c):
    with banco() as conn:
        return [{"id": r["id"], "relatorio": r["relatorio"], "inicio": fmt_ts(r["inicio"]), "fim": fmt_ts(r["fim"]),
                 "minutos": (r["fim"] - r["inicio"]) // 60, "motivo": r["motivo"], "criado_por": r["criado_por"],
                 "criado_em": fmt_ts(r["criado_em"])}
                for r in conn.execute("SELECT * FROM lacunas ORDER BY inicio DESC LIMIT 500")]


@rota("POST", "/lacunas")
def r_lacuna_nova(c):
    ini, fim = val_intervalo(c.b)
    motivo = texto(c.b, "motivo", "o motivo")
    argv = ["disp.py", "lacuna", f"--inicio={ini}", f"--fim={fim}", f"--motivo={motivo}", f"--por={c.usuario}"]
    rel = (c.b.get("relatorio") or "").strip()
    with banco() as conn:
        if rel:
            if rel not in relatorios_medidos(conn):
                raise Erro(400, "medição desconhecida")
            argv.append(f"--relatorio={rel}")
        aviso = aviso_fechados(meses_fechados(conn, disp.epoch(ini), disp.epoch(fim)))
    executar(*argv)
    c.registrar("lacuna-nova", relatorio=rel or None, inicio=ini, fim=fim, motivo=motivo)
    return {"mensagem": "Lacuna registrada", "aviso": aviso}


@rota("POST", r"/lacunas/(\d+)/remover")
def r_lacuna_remover(c, lid):
    with banco() as conn:
        r = conn.execute("SELECT * FROM lacunas WHERE id=?", (int(lid),)).fetchone()
        if not r:
            raise Erro(404, "lacuna não encontrada")
        antes = dict(r)
        conn.execute("DELETE FROM lacunas WHERE id=?", (int(lid),))
        conn.execute("INSERT INTO auditoria (tabela, registro, acao, antes, depois) VALUES ('lacunas',?,'delete',?,?)",
                     (int(lid), json.dumps(antes, ensure_ascii=False), json.dumps({"por": c.usuario}, ensure_ascii=False)))
        conn.commit()
        aviso = aviso_fechados(meses_fechados(conn, r["inicio"], r["fim"]))
    c.registrar("lacuna-remover", **antes)
    return {"mensagem": "Lacuna removida", "aviso": aviso}


@rota("GET", "/itens")
def r_itens(c):
    rel = c.q.get("relatorio")
    sql, args = "SELECT * FROM objetos WHERE 1=1", []
    if rel:
        sql += " AND relatorio=?"
        args.append(rel)
    if c.q.get("estado") == "encerrados":
        sql += " AND ativo_ate IS NOT NULL"
    elif c.q.get("estado") != "todos":
        sql += " AND ativo_ate IS NULL"
    mes = val_mes(c.q.get("mes") or mes_atual())
    with banco() as conn:
        linhas = conn.execute(sql + " ORDER BY relatorio, grupo, item", args).fetchall()
        disponib = {}
        for r in ({rel} if rel else {x["relatorio"] for x in linhas}):
            for l in disp.calcular(conn, r, mes):
                disponib[l["objeto_id"]] = l
    out = []
    for r in linhas:
        l = disponib.get(r["id"]) or {}
        out.append({"id": r["id"], "relatorio": r["relatorio"], "item": r["item"], "grupo": r["grupo"],
                    "descricao": r["descricao"], "triggerid": r["zabbix_triggerid"], "desde": fmt_ts(r["ativo_desde"]),
                    "ate": fmt_ts(r["ativo_ate"]), "disponibilidade": api.pct(l.get("disponibilidade")),
                    "minutos_fora": (l.get("segundos_fora", 0) - l.get("segundos_expurgo", 0)) // 60,
                    "quedas": l.get("incidentes", 0),
                    "macro_pendente": "{$" in (r["item"] or "") or "{$" in (r["grupo"] or "")})
    return out


@rota("GET", r"/itens/(\d+)/remocao")
def r_item_remocao(c, oid):
    with banco() as conn:
        d = disp.contagem_item(conn, int(oid))
    if not d:
        raise Erro(404, "item não encontrado")
    return d


@rota("POST", r"/itens/(\d+)/remover")
def r_item_remover(c, oid):
    with banco() as conn:
        d = disp.contagem_item(conn, int(oid))
    if not d:
        raise Erro(404, "item não encontrado")
    incluir = c.b.get("incluir_fechados") is True
    if d["ativo"] or (d["meses_fechados"] and not incluir):     # recusa antes de fazer uma cópia à toa
        with banco() as conn:
            disp.remover_item(conn, int(oid), c.usuario, incluir)
    copia = copiar_banco("antes-de-remover-item")
    argv = ["disp.py", "remover-item", f"--id={int(oid)}", f"--por={c.usuario}"]
    saida = executar(*argv, *(["--incluir-fechados"] if incluir else []))
    c.registrar("item-remover", copia=copia.name, incluir_fechados=incluir, **{k: d[k] for k in (
        "relatorio", "item", "incidentes", "comentarios", "expurgos", "chamados", "meses_fechados")})
    return {"mensagem": f"{saida} Cópia do banco antes da remoção: {copia.name}",
            "aviso": aviso_fechados(d["meses_fechados"])}


@rota("GET", "/fechamentos")
def r_fechamentos(c):
    mes = val_mes(c.q.get("mes") or mes_anterior())
    terminou = time.time() >= disp.limites_mes(mes)[1]
    base = pasta_pdf().resolve()
    with banco() as conn:
        linhas = conn.execute("SELECT * FROM fechamentos WHERE mes=? ORDER BY relatorio, versao DESC", (mes,)).fetchall()
    por_rel = {}
    for r in linhas:
        p = Path(r["arquivo_pdf"]) if r["arquivo_pdf"] else None
        relativo = None
        if p and p.exists():
            try:
                relativo = p.resolve().relative_to(base).as_posix()
            except ValueError:
                pass
        por_rel.setdefault(r["relatorio"], []).append(
            {"versao": r["versao"], "gerado_em": fmt_ts(r["gerado_em"]), "gerado_por": r["gerado_por"], "pdf": relativo})
    defs = definicoes()
    out = [{"id": k, "titulo": v.get("titulo", k), "versoes": por_rel.pop(k, [])} for k, v in defs.items()]
    out += [{"id": k, "titulo": "(definição removida)", "versoes": v} for k, v in por_rel.items()]
    return {"mes": mes, "terminou": terminou, "relatorios": out}


def caminho_pdf(relativo):
    base = pasta_pdf().resolve()
    if not isinstance(relativo, str) or not relativo:
        raise Erro(400, "arquivo não informado")
    p = (base / relativo).resolve()
    if base not in p.parents or p.suffix.lower() != ".pdf" or not p.is_file():
        raise Erro(404, "arquivo não encontrado")
    return base, p


@rota("GET", "/pdfs")
def r_pdfs(c):
    base = pasta_pdf()
    if not base.exists():
        return []
    out = []
    for p in base.rglob("*.pdf"):
        rel = p.relative_to(base).as_posix()
        partes = rel.split("/")
        tipo = "avulso" if partes[0] == "avulsos" else ("parcial" if "parciais" in partes else "oficial")
        st = p.stat()
        out.append({"caminho": rel, "nome": p.name, "pasta": "/".join(partes[:-1]), "tipo": tipo, "tamanho": st.st_size,
                    "gerado_em": fmt_ts(st.st_mtime), "ordem": st.st_mtime})
    out.sort(key=lambda x: -x["ordem"])
    return out


@rota("GET", "/pdf")
def r_pdf(c):
    _, p = caminho_pdf(c.q.get("caminho"))
    return Arquivo(p, "application/pdf", p.name)


@rota("POST", "/pdfs/remover")
def r_pdf_remover(c):
    base, p = caminho_pdf(c.b.get("caminho"))
    rel = p.relative_to(base)
    if rel.parts[0] != "avulsos":
        raise Erro(403, "só os PDFs avulsos podem ser removidos pelo painel")
    p.unlink()
    if p.parent != base / "avulsos" and not any(p.parent.iterdir()):
        p.parent.rmdir()
    c.registrar("pdf-remover", caminho=rel.as_posix())
    return {"mensagem": "PDF removido"}


@rota("GET", "/definicoes")
def r_definicoes(c):
    with banco() as conn:
        return {"definicoes": definicoes(), "medicoes": relatorios_medidos(conn)}


@rota("POST", r"/definicoes/([a-z0-9][a-z0-9_-]{0,60})")
def r_definicao_gravar(c, rid):
    d = c.b.get("definicao")
    erros = validar_definicao(d)
    if erros:
        raise Erro(400, "; ".join(erros))
    defs = definicoes()
    novo = rid not in defs
    if novo and not c.b.get("novo"):
        raise Erro(404, "relatório não existe mais")
    if not novo and c.b.get("novo"):
        raise Erro(409, "já existe um relatório com este id")
    defs[rid] = d
    gravar_definicoes(defs)
    c.registrar("definicao-gravar", relatorio=rid, novo=novo)
    with banco() as conn:
        faltam = sorted(medicoes_da_definicao(d) - set(relatorios_medidos(conn)))
    return {"mensagem": "Relatório criado" if novo else "Definição gravada",
            "aviso": f"Sem itens no banco para: {', '.join(faltam)}. Confira a tag relatorio no Zabbix." if faltam else None}


@rota("POST", r"/definicoes/([a-z0-9][a-z0-9_-]{0,60})/remover")
def r_definicao_remover(c, rid):
    defs = definicoes()
    if rid not in defs:
        raise Erro(404, "relatório não encontrado")
    del defs[rid]
    gravar_definicoes(defs)
    c.registrar("definicao-remover", relatorio=rid)
    return {"mensagem": "Relatório removido. Os PDFs e fechamentos já feitos continuam guardados."}


@rota("GET", "/tarefas")
def r_tarefas(c):
    with _lock_tarefas:
        return [tarefa_publica(t) for t in reversed(_tarefas.values())]


@rota("POST", "/tarefas")
def r_tarefa_nova(c):
    tipo = c.b.get("tipo")
    p = c.b.get("parametros") or {}
    if not isinstance(tipo, str) or not isinstance(p, dict):
        raise Erro(400, "pedido inválido")
    titulo, argv = montar_tarefa(tipo, p, c.usuario)
    return tarefa_publica(iniciar_tarefa(tipo, titulo, argv, c.usuario, c.ip))


@rota("GET", r"/tarefas/(\d+)")
def r_tarefa(c, tid):
    t = _tarefas.get(int(tid))
    if not t:
        raise Erro(404, "tarefa não encontrada")
    return tarefa_publica(t, val_inteiro(c.q.get("desde") or 0, "desde", 0, 10**9))


@rota("POST", r"/tarefas/(\d+)/cancelar")
def r_tarefa_cancelar(c, tid):
    t = _tarefas.get(int(tid))
    if not t or t["estado"] != "executando" or not t["proc"]:
        raise Erro(409, "esta tarefa não está em execução")
    t["cancelada"] = True
    t["proc"].terminate()
    c.registrar("tarefa-cancelar", tarefa=t["tipo"], titulo=t["titulo"])
    return {"mensagem": "Cancelamento pedido"}


@rota("GET", "/servicos")
def r_servicos(c):
    return estado_unidades()


@rota("POST", r"/servicos/([a-z0-9.-]+)/(start|stop|restart|enable|disable)")
def r_servico_acao(c, nome, acao):
    u = UNIDADES.get(nome)
    if not u or acao not in ACOES_UNIDADE[u["tipo"]]:
        raise Erro(403, "ação não permitida nesta unidade")
    if not shutil.which("systemctl"):
        raise Erro(400, "systemctl não está disponível neste computador")
    argv = ["systemctl", acao, nome] if acao in ("enable", "disable") else ["systemctl", "--no-block", acao, nome]
    if acao in ("enable", "disable"):
        argv.insert(2, "--now")
    cod, _, err = rodar_sistema(argv, 30)
    c.registrar("servico", unidade=nome, operacao=acao, codigo=cod)
    if cod != 0:
        raise Erro(400, err.strip() or f"systemctl falhou (código {cod})")
    return {"mensagem": f"{nome}: {acao} pedido"}


@rota("GET", "/logs")
def r_logs(c):
    nome = c.q.get("unidade")
    if nome not in UNIDADES:
        raise Erro(400, "unidade desconhecida")
    if not shutil.which("journalctl"):
        raise Erro(400, "journalctl não está disponível neste computador")
    n = val_inteiro(c.q.get("linhas") or 200, "linhas", 10, 5000)
    cod, saida, err = rodar_sistema(["journalctl", "-u", nome, "-n", str(n), "--no-pager", "-o", "short-iso"], 30)
    if cod != 0:
        raise Erro(400, err.strip() or "não foi possível ler o journal")
    return {"linhas": saida.splitlines()}


@rota("GET", "/config")
def r_config(c):
    env = ler_env()
    campos = []
    for k, campo in ENV_CAMPOS.items():
        definido = bool(env.get(k))
        campos.append({"chave": k, "rotulo": campo["rotulo"], "ajuda": campo.get("ajuda"), "segredo": bool(campo.get("segredo")),
                       "definido": definido, "valor": None if campo.get("segredo") else env.get(k, "")})
    return {"campos": campos, "arquivo": str(ENV_FILE), "existe": ENV_FILE.exists()}


@rota("POST", "/config")
def r_config_gravar(c):
    valores, remover = c.b.get("valores") or {}, c.b.get("remover") or []
    if not isinstance(valores, dict) or not isinstance(remover, list):
        raise Erro(400, "pedido inválido")
    alterar_env(valores, remover)
    c.registrar("config-gravar", alteradas=sorted(valores), removidas=sorted(remover))   # nunca os valores
    return {"mensagem": "Configuração gravada. As tarefas já usam os novos valores.",
            "aviso": "Reinicie a API em Serviços se mudou o banco ou o token da API."}


@rota("GET", "/backups")
def r_backups(c):
    if not BACKUPS.exists():
        return []
    out = [{"nome": p.name, "tamanho": p.stat().st_size, "criado_em": fmt_ts(p.stat().st_mtime),
            "tipo": "banco" if p.suffix == ".db" else "definições", "ordem": p.stat().st_mtime}
           for p in BACKUPS.iterdir() if p.is_file() and p.suffix in (".db", ".json")]
    return sorted(out, key=lambda x: -x["ordem"])


def caminho_backup(nome):
    if not isinstance(nome, str) or not re.fullmatch(r"[A-Za-z0-9._-]+\.(db|json)", nome):
        raise Erro(400, "nome inválido")
    p = BACKUPS / nome
    if not p.is_file():
        raise Erro(404, "cópia não encontrada")
    return p


def copiar_banco(nome):
    BACKUPS.mkdir(exist_ok=True)
    destino = BACKUPS / f"{nome}-{carimbo()}.db"
    origem = sqlite3.connect(caminho_db(), timeout=30)
    copia = sqlite3.connect(destino)
    try:
        origem.backup(copia)   # cópia consistente mesmo com o banco em uso
    finally:
        copia.close()
        origem.close()
    os.chmod(destino, 0o600)
    return destino


@rota("POST", "/backups")
def r_backup_novo(c):
    destino = copiar_banco("disponibilidade")
    c.registrar("backup-criar", arquivo=destino.name)
    return {"mensagem": f"Cópia criada: {destino.name}"}


@rota("GET", "/backup")
def r_backup_baixar(c):
    p = caminho_backup(c.q.get("nome"))
    c.registrar("backup-baixar", arquivo=p.name)
    return Arquivo(p, "application/octet-stream", p.name)


@rota("POST", "/backups/remover")
def r_backup_remover(c):
    p = caminho_backup(c.b.get("nome"))
    p.unlink()
    c.registrar("backup-remover", arquivo=p.name)
    return {"mensagem": "Cópia removida"}


@rota("GET", "/auditoria")
def r_auditoria(c):
    painel = []
    if LOG_FILE.exists():
        with LOG_FILE.open(encoding="utf-8") as f:
            for linha in collections.deque(f, maxlen=500):
                try:
                    painel.append(json.loads(linha))
                except ValueError:
                    pass
    painel.reverse()
    with banco() as conn:
        banco_linhas = [{"em": fmt_ts(r["em"]), "tabela": r["tabela"], "registro": r["registro"], "acao": r["acao"],
                         "antes": r["antes"], "depois": r["depois"]}
                        for r in conn.execute("SELECT * FROM auditoria ORDER BY id DESC LIMIT 300")]
    return {"painel": painel, "banco": banco_linhas}


@rota("GET", "/usuarios")
def r_usuarios(c):
    return [{"nome": k, "criado_em": v.get("criado_em"), "senha_em": v.get("senha_em"), "voce": k == c.usuario}
            for k, v in sorted(ler_usuarios().items())]


@rota("POST", "/usuarios")
def r_usuario_novo(c):
    nome = c.b.get("nome")
    validar_nome_usuario(nome)
    if nome in ler_usuarios():
        raise Erro(409, "este usuário já existe")
    definir_senha(nome, c.b.get("senha"))
    c.registrar("usuario-criar", nome=nome)
    return {"mensagem": f"Usuário {nome} criado"}


@rota("POST", r"/usuarios/([a-zA-Z0-9._-]{2,32})/senha")
def r_usuario_senha(c, nome):
    if nome not in ler_usuarios():
        raise Erro(404, "usuário não encontrado")
    if nome == c.usuario and not confere_senha(c.b.get("atual") or "", ler_usuarios()[nome]["hash"]):
        raise Erro(403, "a senha atual não confere")
    definir_senha(nome, c.b.get("senha"))
    encerrar_sessoes(nome, exceto=c.token)
    c.registrar("usuario-senha", nome=nome)
    return {"mensagem": "Senha alterada"}


@rota("POST", r"/usuarios/([a-zA-Z0-9._-]{2,32})/remover")
def r_usuario_remover(c, nome):
    if nome == c.usuario:
        raise Erro(400, "você não pode remover o próprio usuário")
    with _lock_arquivos:
        us = ler_usuarios()
        if nome not in us:
            raise Erro(404, "usuário não encontrado")
        del us[nome]
        gravar_usuarios(us)
    encerrar_sessoes(nome)
    c.registrar("usuario-remover", nome=nome)
    return {"mensagem": f"Usuário {nome} removido"}


# ---------------------------------------------------------- HTTP ----

class Handler(BaseHTTPRequestHandler):
    server_version = "ENGIE-Admin"
    sys_version = ""
    timeout = 120
    seguro = False

    def cabecalhos(self):
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")

    def json(self, status, corpo, cookie=None):
        b = json.dumps(corpo, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.cabecalhos()
        self.end_headers()
        self.wfile.write(b)

    def arquivo(self, a):
        self.send_response(200)
        self.send_header("Content-Type", a.tipo)
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{urllib.parse.quote(a.nome)}")
        self.send_header("Content-Length", str(a.caminho.stat().st_size))
        self.cabecalhos()
        self.end_headers()
        with a.caminho.open("rb") as f:
            shutil.copyfileobj(f, self.wfile)

    def estatico(self, caminho):
        nome, tipo = ESTATICOS[caminho]
        b = (WEB / nome).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(b)))
        self.cabecalhos()
        self.end_headers()
        self.wfile.write(b)

    def montar_cookie(self, valor, idade):
        return (f"{COOKIE}={valor}; Path=/; HttpOnly; SameSite=Strict; Max-Age={idade}" + ("; Secure" if self.seguro else ""))

    def token(self):
        for parte in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = parte.strip().partition("=")
            if k == COOKIE:
                return v
        return None

    def corpo(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError as e:
            raise Erro(400, "Content-Length inválido") from e
        if n > CORPO_MAX:
            raise Erro(413, "pedido grande demais")
        if not n:
            return {}
        try:
            d = json.loads(self.rfile.read(n))
        except ValueError as e:
            raise Erro(400, "JSON inválido") from e
        if not isinstance(d, dict):
            raise Erro(400, "o corpo precisa ser um objeto JSON")
        return d

    def checar_origem(self):
        """Só o próprio painel consegue enviar: o header exige preflight, que nunca é autorizado."""
        tipo = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if self.headers.get("X-Painel") != "1" or tipo != "application/json":
            raise Erro(403, "pedido recusado")
        origem = self.headers.get("Origin")
        if origem and urllib.parse.urlparse(origem).netloc != self.headers.get("Host"):
            raise Erro(403, "origem recusada")

    def login(self):
        d = self.corpo()
        nome, senha = d.get("usuario"), d.get("senha")
        ip = self.client_address[0]
        if not isinstance(nome, str) or not isinstance(senha, str):
            raise Erro(400, "informe usuário e senha")
        chave = f"{ip}|{nome.lower()}"
        if bloqueado(chave):
            raise Erro(429, "muitas tentativas: aguarde 5 minutos")
        u = ler_usuarios().get(nome)
        ok = confere_senha(senha, u["hash"] if u else SENHA_FALSA)
        if not (ok and u):
            registrar_falha(chave)
            auditar(nome[:64], "login-falhou", ip)
            raise Erro(401, "usuário ou senha incorretos")
        auditar(nome, "login", ip)
        self.json(200, {"usuario": nome}, self.montar_cookie(criar_sessao(nome), SESSAO_SEG))

    def tratar(self, metodo):
        u = urllib.parse.urlparse(self.path)
        try:
            if metodo == "GET" and u.path in ESTATICOS:
                return self.estatico(u.path)
            if not u.path.startswith("/api/"):
                return self.json(404, {"erro": "não encontrado"})
            if metodo == "POST":
                self.checar_origem()
            if metodo == "POST" and u.path == "/api/login":
                return self.login()
            token = self.token()
            usuario = usuario_da_sessao(token)
            if u.path == "/api/sessao" and metodo == "GET":
                return self.json(200, {"usuario": usuario})
            if not usuario:
                return self.json(401, {"erro": "sessão expirada: entre novamente"})
            if metodo == "POST" and u.path == "/api/logout":
                with _lock_sessoes:
                    _sessoes.pop(token, None)
                auditar(usuario, "logout", self.client_address[0])
                return self.json(200, {"ok": True}, self.montar_cookie("", 0))
            for m, rx, f in ROTAS:
                hit = rx.match(u.path) if m == metodo else None
                if hit:
                    q = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
                    ctx = Ctx(q, self.corpo() if metodo == "POST" else {}, usuario, self.client_address[0], token)
                    r = f(ctx, *hit.groups())
                    return self.arquivo(r) if isinstance(r, Arquivo) else self.json(200, r)
            self.json(404, {"erro": "rota inexistente"})
        except Erro as e:
            self.json(e.status, {"erro": e.mensagem})
        except SystemExit as e:             # disp.py sinaliza dado inválido com SystemExit
            self.json(400, {"erro": str(e.code)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.json(500, {"erro": f"erro interno: {e}"})

    def do_GET(self):
        self.tratar("GET")

    def do_POST(self):
        self.tratar("POST")

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.address_string()} {fmt % args}\n")


class Servidor(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        erro = sys.exc_info()[1]
        if not isinstance(erro, (ssl.SSLError, ConnectionError, TimeoutError)):
            super().handle_error(request, client_address)


def servidor():
    if not ler_usuarios():
        raise SystemExit("Nenhum usuário cadastrado. Crie o primeiro com: python3 admin.py usuario NOME")
    host = os.getenv("ADMIN_HOST") or os.getenv("SERVIDOR_IP") or "127.0.0.1"
    porta = int(os.getenv("ADMIN_PORT", "8098"))
    srv = Servidor((host, porta), Handler)
    cert, chave = os.getenv("ADMIN_TLS_CERT"), os.getenv("ADMIN_TLS_KEY")
    if cert:
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, chave)
        srv.socket = tls.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
        Handler.seguro = True
    print(f"Painel em {'https' if cert else 'http'}://{host}:{porta} (banco: {caminho_db()})", flush=True)
    srv.serve_forever()


# ----------------------------------------------- comandos auxiliares ----

def cmd_usuario(nome):
    if sys.stdin.isatty():
        senha = getpass.getpass("Senha: ")
        if senha != getpass.getpass("Repita a senha: "):
            raise SystemExit("As senhas não conferem")
    else:
        senha = sys.stdin.readline().rstrip("\r\n")
    try:
        novo = definir_senha(nome, senha)
    except Erro as e:
        raise SystemExit(e.mensagem) from e
    auditar("linha de comando", "usuario-criar" if novo else "usuario-senha", nome=nome)
    print(f"Usuário {nome} {'criado' if novo else 'atualizado'}.")


def cmd_testar(alvo):
    os.environ.update(ambiente())
    if alvo == "zabbix":
        n = disp.Zabbix().call("host.get", {"countOutput": True})
        print(f"Zabbix OK: o token enxerga {n} host(s).")
    else:
        r = disp.TopDesk().get("version")
        if r is None:
            raise SystemExit("TOPdesk respondeu, mas sem a versão: confira a URL.")
        print(f"TOPdesk OK: versão {r.get('version', r)}.")


def cmd_carga_inicial(desde):
    """Primeira carga: mesmos comandos do dia a dia, com o histórico desde a data informada."""
    val_data(desde, "início da medição")
    env = ambiente()
    hoje = datetime.now(disp.TZ)
    dias = max(45, (hoje.date() - datetime.strptime(desde, "%Y-%m-%d").date()).days + 1)
    passos = [("Banco e itens do Zabbix", ["sync-objetos", "--desde", desde]),
              ("Quedas", ["importar-eventos", "--de", desde, "--ate", hoje.strftime("%Y-%m-%d")])]
    if env.get("TOPDESK_URL"):
        passos.append(("Chamados do TOPdesk", ["importar-chamados", "--dias", str(dias)]))
    else:
        print("TOPdesk não configurado: os chamados ficam para depois.", flush=True)
    for titulo, args in passos:
        print(f"== {titulo}", flush=True)
        r = subprocess.run([sys.executable, str(BASE / "disp.py"), *args], cwd=BASE, env=env)
        if r.returncode != 0:
            raise SystemExit(f"Parou em: {titulo}. Corrija e rode a carga inicial de novo; ela não duplica dados.")
    print("Carga inicial concluída.")


def main():
    ap = argparse.ArgumentParser(description="Painel de administração dos Relatórios ENGIE")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("servidor")
    s = sub.add_parser("usuario")
    s.add_argument("nome")
    s = sub.add_parser("testar")
    s.add_argument("alvo", choices=["zabbix", "topdesk"])
    s = sub.add_parser("carga-inicial")
    s.add_argument("desde")
    a = ap.parse_args()
    if a.cmd == "usuario":
        cmd_usuario(a.nome)
    elif a.cmd == "testar":
        cmd_testar(a.alvo)
    elif a.cmd == "carga-inicial":
        try:
            cmd_carga_inicial(a.desde)
        except Erro as e:
            raise SystemExit(e.mensagem) from e
    else:
        servidor()


if __name__ == "__main__":
    main()
