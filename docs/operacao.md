# Operação

O dia a dia é feito no painel, em `http://IP_DO_SERVIDOR:8098`. Toda ação fica registrada em Auditoria com o nome de quem fez.

## Como funciona

A cada 10 minutos, o `atualizar.sh` sincroniza os itens, importa quedas e chamados e apaga dados com mais de 3 meses fechados. A API e os PDFs só leem o banco, nunca o Zabbix.

| Serviço | Quando | O que faz |
| --- | --- | --- |
| `relatorios-engie-api.service` | Sempre | API na porta 8099, para o Grafana |
| `relatorios-engie-admin.service` | Sempre | Painel na porta 8098 |
| `relatorios-engie-atualizar.timer` | A cada 10 min | Atualiza o banco |
| `relatorios-engie-parciais.timer` | Todo dia às 05:30 | PDF parcial do mês em `pdf/AAAA/MM/parciais` |
| `relatorios-engie-fechamento.timer` | Dia 1º às 06:00 | Fecha o mês anterior: PDF oficial em `pdf/AAAA/MM` e valores imutáveis no banco |

Todos os PDFs ficam em Arquivos PDF, no painel. O sistema não envia e-mail.

## Tarefas comuns

| Tarefa | No painel |
| --- | --- |
| Não contabilizar quedas de um item | Quedas → escolha o item → marque → Desconsiderar |
| Não contabilizar as quedas de vários chamados | Quedas → Por lista de chamados: cole os números, confira e confirme |
| Desfazer | Quedas → marque → Voltar a contar |
| Descontar uma manutenção programada | Expurgos → Novo expurgo de janela, depois Aprovar |
| Registrar período sem monitoramento | Lacunas → Registrar lacuna |
| Gerar um relatório de um período (últimos 7, 15 ou 30 dias, semana ou mês passado, 3 meses, ou outras datas) | Gerar relatório |
| Baixar um PDF | Arquivos PDF |
| Rever um mês já fechado | Fechamento → Nova versão |
| Novo relatório | Definições → Novo relatório |
| Forçar uma atualização | Painel → Atualizar tudo agora |
| Ver logs ou reiniciar um serviço | Serviços e logs |
| Mudar token ou senha | Configuração |
| Copiar o banco | Backups |

Três regras importantes:

- **Itens vêm do Zabbix.** Para incluir, tirar ou renomear, altere as tags lá. Tirar o template ou a tag de um host encerra o item no próximo sincronismo: ele continua nos meses em que existiu, inclusive o atual, e some dos seguintes. Para apagá-lo de todos os períodos, use Itens medidos → Remover um item. O painel só remove item encerrado e faz uma cópia do banco antes (em Backups); para desfazer, restaure essa cópia. Quedas do item em meses fechados só saem do PDF oficial com uma nova versão.
- **Mês fechado não muda sozinho.** Alterar expurgo ou lacuna de um mês fechado não muda o PDF oficial. O painel avisa, e a correção é gerar uma nova versão.

- **Período antigo não tem queda.** A limpeza automática apaga as quedas com mais de 3 meses fechados. Um relatório desse período sairia com disponibilidade maior do que foi de verdade; o painel avisa antes de gerar. Para meses já fechados, vale o PDF oficial guardado em Fechamento.

Um relatório novo usa as medições já existentes ou um id novo marcado nas triggers. Ele entra sozinho no parcial diário e no fechamento.

## Regra de cálculo

Por item e por mês, no horário de Brasília:

```
disponibilidade = 1 − (tempo fora − tempo expurgado) / (tempo do mês − lacunas)
```

- Quedas sobrepostas contam uma vez.
- Só expurgos **aprovados** descontam, e só dentro do tempo fora.
- O mês corrente é calculado até agora; itens novos contam a partir de quando passaram a ser medidos.
- O grupo é a média simples dos itens.
- Percentuais são cortados em 2 casas, nunca arredondados para 100%. A meta é 99,90%.

## Trocar um IP

Os endereços ficam em `/etc/relatorios-engie/env`. Todas as variáveis estão explicadas em `env.exemplo`, na mesma pasta.

| Variável | Para quê |
| --- | --- |
| `SERVIDOR_IP` | Onde a API e o painel atendem; também vai nos dashboards |
| `ZABBIX_URL` | Endereço do frontend do Zabbix |
| `ZABBIX_FRONTEND_IP` | Opcional: IP do nome do Zabbix no `/etc/hosts` |
| `GRAFANA_URL` | Opcional: atalho do Grafana no painel |

Para mudar `SERVIDOR_IP` ou `ZABBIX_FRONTEND_IP`, edite o arquivo no servidor e rode o `instalar.sh` de novo. Ele ajusta o `/etc/hosts`, refaz os dashboards em `/etc/relatorios-engie/grafana` e reinicia os serviços. Se `SERVIDOR_IP` mudou, importe os dashboards de novo no Grafana. Essas duas variáveis não aparecem no painel, porque um IP errado o deixaria fora do ar.

## Segurança do painel

| Ponto | Como é |
| --- | --- |
| Acesso | Todos os usuários têm acesso total |
| Privilégio | O serviço roda como root, porque controla o systemd e edita o `env` |
| Segredos | Token e senhas nunca são enviados ao navegador; só podem ser substituídos |
| Comandos | O navegador só escolhe tarefas de uma lista fixa, com os parâmetros conferidos |
| Auditoria | `admin_auditoria.log` não é apagado pela limpeza do banco |

Esqueceu a senha? No servidor: `cd /etc/relatorios-engie && python3 admin.py usuario SEU_NOME`.

## Problemas comuns

| Sintoma | Causa provável e correção |
| --- | --- |
| `CERTIFICATE_VERIFY_FAILED` nos logs | Em Configuração, ponha 0 em "Verificar certificado" |
| Erro de conexão com o domínio do Zabbix | Defina `ZABBIX_FRONTEND_IP` no `env` e rode o `instalar.sh` de novo: ele ajusta o `/etc/hosts` |
| Selo "macro sem valor" no item | Host sem a macro; preencha e aguarde 10 min |
| Dashboard sem dados | UID do Infinity diferente do JSON, ou porta 8099 bloqueada a partir do Grafana |
| Relatório com itens a mais ou a menos | Confira tags e macros; itens sem trigger são encerrados no próximo sincronismo |
| Fechamento não gerou um relatório | Serviços e logs → Fechamento → Logs; um erro não impede os demais. Depois, feche por Fechamento |
| Painel não abre | No servidor: `systemctl status relatorios-engie-admin` e `journalctl -u relatorios-engie-admin -n 30` |

## Pela linha de comando

Para quando o painel não estiver disponível:

```bash
cd /etc/relatorios-engie && set -a && . ./env && set +a

python3 disp.py incidentes --relatorio links-internet --mes 2026-11
python3 disp.py desconsiderar --chamado I2611-12345 --motivo "..." --por "Nome"
python3 disp.py contabilizar --chamado I2611-12345 --por "Nome"
python3 disp.py expurgos --mes 2026-11
python3 disp.py lacuna --inicio "2026-10-25 18:10" --fim "2026-10-25 19:30" --motivo "..." --por "Nome"
python3 relatorio.py gerar --relatorio tid --mes 2026-11 --saida /tmp/tid.pdf
python3 relatorio.py fechar --relatorio tid --mes 2026-11 --nova-versao
```

Cópia do banco: `sqlite3 /etc/relatorios-engie/disponibilidade.db ".backup /root/disponibilidade-$(date +%F).db"`.

Para restaurar uma cópia, pare os serviços, substitua o arquivo do banco e religue:

```bash
systemctl stop relatorios-engie-atualizar.timer relatorios-engie-api relatorios-engie-admin
rm -f /etc/relatorios-engie/disponibilidade.db-wal /etc/relatorios-engie/disponibilidade.db-shm
cp COPIA.db /etc/relatorios-engie/disponibilidade.db
systemctl start relatorios-engie-api relatorios-engie-admin relatorios-engie-atualizar.timer
```
