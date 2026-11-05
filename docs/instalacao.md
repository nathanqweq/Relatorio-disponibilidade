# Instalação

Leva cerca de 30 minutos. Faça na ordem: o Zabbix primeiro, porque a instalação já importa os dados dele.

| Servidor | Papel |
| --- | --- |
| Servidor do Zabbix (`IP_DO_SERVIDOR`) | Onde este sistema é instalado: banco SQLite, API (porta 8099), painel (porta 8098), PDFs, timers |
| Frontend (`IP_DO_FRONTEND`) | Frontend do Zabbix e Grafana |
| Banco do Zabbix | PostgreSQL do Zabbix; o sistema não acessa direto |

Os endereços reais são informados na instalação e ficam só no arquivo `env` do servidor.

## 1. O que ter em mãos

| O que | Onde conseguir |
| --- | --- |
| Token de API do Zabbix | Zabbix: Users → API tokens, com usuário que leia os grupos dos relatórios |
| Usuário e senha de aplicação do TOPdesk | TOPdesk: perfil do operador → Senhas de aplicativo, com permissão de API REST. Pode ficar para depois |

Rede necessária:

- Servidor → frontend do Zabbix em HTTPS.
- Servidor → TOPdesk em HTTPS.
- Grafana → servidor, porta 8099.
- Sua rede interna ou VPN → servidor, porta 8098 (painel). Não exponha essa porta fora da rede interna.

## 2. Marcar as triggers no Zabbix

O sistema só mede o que tiver a tag `relatorio` numa trigger. Cada trigger marcada vira um item do relatório, e os problemas dela viram as quedas.

| Tag | Obrigatória | Valor |
| --- | --- | --- |
| `relatorio` | Sim | Id da medição (pode repetir a tag para entrar em mais de uma) |
| `item` | Não (padrão: nome do host) | Nome do item; use `{HOST.NAME}` no template |
| `grupo` | Não | Operadora, Geração/Transmissão, aplicação. Sem a tag na trigger, vale a tag `grupo` do host |

Os valores aceitam `{HOST.NAME}`, `{HOST.HOST}` e macros de usuário (`{$...}`), resolvidas com a herança do Zabbix: host, depois templates, depois global.

| Medição | Onde fica a trigger | Tags |
| --- | --- | --- |
| `tid-usinas` | Templates ENGIE - Core Status Geracao / Transmicao, trigger Powerplant Down | `relatorio=tid-usinas`, `grupo=GERACAO` (ou `TRANSMISSAO`), `item={HOST.NAME}` |
| `tid-aplicacoes` | Hosts ENGIE APP - TID \* | Criadas pelo painel, depois da instalação: Tarefas → Criar triggers TID |
| `links-internet` e `links-mpls` | Template ENGIE - Template link by IP SLA, trigger Neighbor BGP - Link fora do ar | `relatorio={$LINK.RELATORIO}`, `grupo={$LINK.OPERADORA}`, `item={HOST.NAME}` |
| `firewalls-sci` | Template FW\_VIP - Generic by SNMP (só nos \*\_VIP), trigger Unavailable by ICMP ping | `relatorio=firewalls-sci`, `item={HOST.NAME}` |

Macros dos links:

- No template: `{$LINK.RELATORIO}` = `links-internet`.
- Nos 36 hosts MPLS: `{$LINK.RELATORIO}` = `links-mpls`. Para tirar um link de todos os relatórios, deixe essa macro vazia no host.
- Em cada host: `{$LINK.OPERADORA}` = operadora em maiúsculas (OI, EBT, ALGAR...), sempre com a mesma grafia.

O número do chamado vem do comentário do godesk no problema ("Chamado criado: I2611-12345"). Para outro formato, defina a regex em Configuração, no painel.

## 3. Instalar

No Windows, gere o pacote e envie ao servidor:

```powershell
powershell -ExecutionPolicy Bypass -File empacotar.ps1
scp dist\ENGIE_Relatorios_Disponibilidade.zip root@IP_DO_SERVIDOR:/tmp/
```

No servidor, como root:

```bash
apt install -y unzip && cd /tmp && unzip -o ENGIE_Relatorios_Disponibilidade.zip
sh ENGIE_Relatorios_Disponibilidade/instalar.sh
```

O instalador pergunta:

| Pergunta | Observação |
| --- | --- |
| IP do servidor | Onde a API e o painel vão atender |
| URL e token do Zabbix | Com o token vazio, a carga inicial fica para depois |
| Verificar o certificado | Responda `n` enquanto o certificado do frontend não tiver a cadeia completa |
| IP do frontend do Zabbix | Vai para o `/etc/hosts`. Deixe vazio se o nome já resolve |
| TOPdesk | Pode ficar vazio e ser preenchido depois |
| Usuário e senha do painel | Senha com no mínimo 10 caracteres |
| Primeiro dia a medir | Início do histórico. O limite é o tempo que o Zabbix guarda eventos |

Ele também instala as dependências, cadastra os itens, importa as quedas e os chamados, e liga a API, o painel e os timers.

Se aparecer algo em "Pendente" no final, corrija e rode a Carga inicial no painel, em Tarefas. Ela pode ser repetida sem duplicar dados.

## 4. Conferir

Entre no painel, em `http://IP_DO_SERVIDOR:8098`:

1. **Painel**: a lista "O que precisa de atenção" deve estar sem erros. Serviços com problema aparecem ali.
2. **Itens medidos**: confira os itens por medição. O selo "macro sem valor" indica macro faltando no host.
3. **Configuração**: use Testar Zabbix e Testar TOPdesk.
4. **Arquivos PDF**: gere um PDF avulso para ver se a geração funciona.

## 5. Dashboards no Grafana

Os dashboards leem a API pelo plugin Infinity e seguem o seletor de tempo do Grafana.

1. Instale o plugin Infinity (Plugins → yesoreyeram-infinity-datasource) e crie um datasource dele, sem autenticação. Se ele tiver lista de hosts permitidos, inclua `http://IP_DO_SERVIDOR:8099`.
2. O instalador gera os dashboards com o endereço da API em `/etc/relatorios-engie/grafana`. Use esses arquivos: os da pasta `grafana/` do repositório têm só um marcador no lugar do endereço. Para trazê-los ao seu computador: `scp root@IP_DO_SERVIDOR:/etc/relatorios-engie/grafana/*.json .`
3. Os JSONs vêm com o UID `dg2gfvwh9s0sgb`. Se o seu datasource tiver outro (veja na URL dele), troque antes de importar: `sed -i 's/dg2gfvwh9s0sgb/NOVO_UID/g' /etc/relatorios-engie/grafana/*.json`.
4. Importe cada arquivo em Dashboards → New → Import. Numa atualização, use Import (Overwrite).

| Arquivo | Cobre |
| --- | --- |
| `ENGIE_Disponibilidade_TID_sqlite.json` | TID, Switches Core GA e NE |
| `ENGIE_Disponibilidade_Links_sqlite.json` | Links Internet, Links BGP e Operadoras |
| `ENGIE_Disponibilidade_Firewalls_SCI_sqlite.json` | Firewalls SCI |

Para refazer os JSONs depois de mudar o visual: `cd grafana && python3 gerar.py`, e rode o `instalar.sh` de novo para gerar os dashboards finais.

## 6. HTTPS no painel (recomendado)

Sem certificado, senhas e tokens digitados no painel trafegam sem criptografia. Para ativar, coloque o certificado no servidor e rode `systemctl edit relatorios-engie-admin.service`, acrescentando:

```ini
[Service]
Environment=ADMIN_TLS_CERT=/etc/relatorios-engie/tls/painel.crt ADMIN_TLS_KEY=/etc/relatorios-engie/tls/painel.key
```

Depois, `systemctl restart relatorios-engie-admin.service` e acesse por `https://`. Essa configuração continua valendo depois das atualizações.

## Atualizar uma instalação existente

Repita o passo 3. Sem nenhuma pergunta, o instalador troca os programas e as unidades e reinicia os serviços. Não são alterados: `env`, banco, PDFs, usuários, backups e auditoria.

As definições editadas no painel também ficam como estão. Se o pacote trouxer uma versão diferente de `relatorios.json`, ela é guardada ao lado, como `relatorios.json.pacote`, para você comparar.

Vindo de uma instalação feita pelo tutorial antigo, o instalador só pergunta o usuário do painel.
