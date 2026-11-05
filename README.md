# Relatórios de Disponibilidade ENGIE

Calcula a disponibilidade mensal a partir dos problemas do Zabbix, guarda tudo num banco SQLite e entrega os números ao Grafana e aos PDFs. A administração é feita por um painel web.

## Instalar ou atualizar

No Windows, gere o pacote e envie ao servidor:

```powershell
powershell -ExecutionPolicy Bypass -File empacotar.ps1
scp dist\ENGIE_Relatorios_Disponibilidade.zip root@IP_DO_SERVIDOR:/tmp/
```

No servidor (Debian 13), como root:

```bash
apt install -y unzip && cd /tmp && unzip -o ENGIE_Relatorios_Disponibilidade.zip
sh ENGIE_Relatorios_Disponibilidade/instalar.sh
```

Na primeira vez, o instalador faz as perguntas necessárias e liga tudo. Para atualizar, repita os mesmos comandos: só os programas são trocados, e credenciais, banco, PDFs e usuários ficam como estão.

O painel fica em `http://IP_DO_SERVIDOR:8098`. Os endereços do ambiente ficam só no arquivo `env` do servidor, que não entra no repositório.

## O que há aqui

| Caminho | Conteúdo | Vai para o servidor |
| --- | --- | --- |
| `instalar.sh`, `instalar.py` | Instalador e atualizador | Sim, no pacote |
| `empacotar.ps1` | Gera o pacote `dist/*.zip` | Não |
| `docs/instalacao.md` | Preparar o Zabbix, instalar, Grafana, HTTPS | Sim, no pacote |
| `docs/operacao.md` | Dia a dia, regra de cálculo, problemas comuns | Sim, no pacote |
| `servidor/` | Programas, painel, serviços e testes | Instalado em `/etc/relatorios-engie` |
| `grafana/` | Dashboards para importar e o gerador deles | Só no pacote, para importar no Grafana |
| `planilha/` | Planilha de blocos e plano dos relatórios | Não (referência) |

Dentro de `servidor/`:

| Arquivo | Função |
| --- | --- |
| `disp.py` | Motor de cálculo, importação do Zabbix e do TOPdesk, comandos |
| `api.py` | API somente leitura para o Grafana (porta 8099) |
| `relatorio.py` | Geração e fechamento dos PDFs |
| `relatorios.json` | Definição de cada relatório |
| `admin.py`, `admin_web/` | Painel de administração (porta 8098) |
| `atualizar.sh`, `systemd/` | Atualização a cada 10 minutos e serviços |
| `criar_triggers_tid.py` | Cria as triggers TID no Zabbix |
| `schema.sql` | Estrutura do banco |
| `testes/` | Testes automáticos |

## Testes

```bash
cd servidor && python3 -m pytest -q testes
```
