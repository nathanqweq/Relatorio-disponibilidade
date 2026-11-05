-- Banco de disponibilidade (SQLite)
-- Datas sempre em epoch UTC (segundos). O mês do relatório é calculado no fuso America/Sao_Paulo.
PRAGMA journal_mode = WAL;        -- a atualização grava enquanto o relatório lê
PRAGMA foreign_keys = ON;

-- Versão do esquema, para migrações futuras
CREATE TABLE IF NOT EXISTS meta (
    chave TEXT PRIMARY KEY,
    valor TEXT NOT NULL
);
INSERT OR IGNORE INTO meta (chave, valor) VALUES ('versao_esquema', '1');

-- Catálogo do que é medido. Cada objeto = uma linha nas tabelas de disponibilidade
-- (uma usina, um link, um neighbor BGP, um firewall...). Precisa existir mesmo sem
-- incidente, senão quem ficou 100% não aparece no relatório.
-- Vem das triggers do Zabbix com tag "relatorio" (comando sync-objetos).
CREATE TABLE IF NOT EXISTS objetos (
    id            INTEGER PRIMARY KEY,
    relatorio     TEXT NOT NULL,             -- tag relatorio  (ex.: links-internet)
    item          TEXT NOT NULL,             -- tag item       (ex.: UHSA_MON_INTERNET_OI)
    grupo         TEXT,                      -- tag grupo/operadora/usina (ex.: OI)
    nome          TEXT,                      -- nome exibido; padrão = item
    descricao     TEXT,                      -- designador, velocidade, contato...
    tags_json     TEXT NOT NULL DEFAULT '{}',-- todas as tags da trigger, para filtros futuros
    zabbix_triggerid TEXT,
    ativo_desde   INTEGER NOT NULL,          -- só conta disponibilidade a partir daqui
    ativo_ate     INTEGER,                   -- desativado (NULL = ativo)
    UNIQUE (relatorio, item)
);

-- Quais triggers alimentam cada objeto (um objeto pode vir de mais de uma trigger,
-- e uma trigger pode alimentar objetos de mais de um relatório)
CREATE TABLE IF NOT EXISTS objeto_triggers (
    objeto_id   INTEGER NOT NULL REFERENCES objetos(id),
    triggerid   TEXT NOT NULL,
    PRIMARY KEY (objeto_id, triggerid)
);
CREATE INDEX IF NOT EXISTS ix_objeto_triggers_trigger ON objeto_triggers (triggerid);

-- Incidentes = problemas do Zabbix (início/fim) ligados a um objeto
CREATE TABLE IF NOT EXISTS incidentes (
    id                 INTEGER PRIMARY KEY,
    objeto_id          INTEGER NOT NULL REFERENCES objetos(id),
    zabbix_eventid     TEXT UNIQUE,          -- evento de problema (evita duplicar)
    zabbix_r_eventid   TEXT,                 -- evento de recuperação
    inicio             INTEGER NOT NULL,
    fim                INTEGER,              -- NULL = ainda aberto
    severidade         INTEGER,
    problema           TEXT,                 -- nome do problema no Zabbix
    chamado            TEXT,                 -- número no TOPdesk
    chamado_externo    TEXT,                 -- protocolo da operadora (opcional)
    causa              TEXT,
    resumo             TEXT,                 -- reservado (não usado hoje)
    origem             TEXT NOT NULL DEFAULT 'zabbix',  -- zabbix | manual
    criado_em          INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    atualizado_em      INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    CHECK (fim IS NULL OR fim >= inicio)
);
CREATE INDEX IF NOT EXISTS ix_incidentes_objeto_periodo ON incidentes (objeto_id, inicio, fim);

-- Comentários do problema no Zabbix (acknowledges): abertura/encerramento de chamado, notas do NOC
CREATE TABLE IF NOT EXISTS comentarios (
    id            INTEGER PRIMARY KEY,
    incidente_id  INTEGER NOT NULL REFERENCES incidentes(id),
    clock         INTEGER NOT NULL,
    usuario       TEXT,
    mensagem      TEXT NOT NULL,
    UNIQUE (incidente_id, clock, mensagem)
);

-- Expurgos = intervalos descontados da indisponibilidade (nunca se mexe no percentual)
CREATE TABLE IF NOT EXISTS expurgos (
    id             INTEGER PRIMARY KEY,
    objeto_id      INTEGER REFERENCES objetos(id),   -- NULL = vale para o relatório inteiro
    relatorio      TEXT,                              -- usado quando objeto_id é NULL
    incidente_id   INTEGER REFERENCES incidentes(id), -- incidente de origem, se houver
    inicio         INTEGER NOT NULL,
    fim            INTEGER NOT NULL,
    tipo           TEXT NOT NULL,     -- desligamento_nao_comunicado | manutencao_programada | energia_concessionaria | outro
    motivo         TEXT NOT NULL,
    chamado        TEXT,
    status         TEXT NOT NULL DEFAULT 'proposto' CHECK (status IN ('proposto','aprovado','rejeitado')),
    proposto_por   TEXT NOT NULL,
    proposto_em    INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    decidido_por   TEXT,
    decidido_em    INTEGER,
    CHECK (fim > inicio),
    CHECK (objeto_id IS NOT NULL OR relatorio IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS ix_expurgos_periodo ON expurgos (status, inicio, fim);

-- Períodos sem monitoramento (Zabbix fora, proxy fora...): saem do cálculo,
-- não contam como disponível nem como indisponível.
CREATE TABLE IF NOT EXISTS lacunas (
    id         INTEGER PRIMARY KEY,
    relatorio  TEXT,                  -- NULL = todos os relatórios
    inicio     INTEGER NOT NULL,
    fim        INTEGER NOT NULL,
    motivo     TEXT NOT NULL,
    criado_por TEXT NOT NULL,
    criado_em  INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    CHECK (fim > inicio)
);

-- Fechamento mensal congelado (versão 1, 2... se houver revisão)
CREATE TABLE IF NOT EXISTS fechamentos (
    id          INTEGER PRIMARY KEY,
    relatorio   TEXT NOT NULL,
    mes         TEXT NOT NULL,        -- AAAA-MM
    versao      INTEGER NOT NULL,
    gerado_em   INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    gerado_por  TEXT NOT NULL,
    arquivo_pdf TEXT,
    observacao  TEXT,
    UNIQUE (relatorio, mes, versao)
);
CREATE TABLE IF NOT EXISTS fechamento_valores (
    fechamento_id      INTEGER NOT NULL REFERENCES fechamentos(id),
    objeto_id          INTEGER NOT NULL REFERENCES objetos(id),
    segundos_periodo   INTEGER NOT NULL,
    segundos_fora      INTEGER NOT NULL,
    segundos_expurgo   INTEGER NOT NULL,
    disponibilidade    REAL,          -- fração 0..1 (NULL = sem dados)
    PRIMARY KEY (fechamento_id, objeto_id)
);

-- Textos digitados todo mês (recomendações, itens de melhoria...)
CREATE TABLE IF NOT EXISTS textos_mensais (
    id         INTEGER PRIMARY KEY,
    relatorio  TEXT NOT NULL,
    mes        TEXT NOT NULL,
    secao      TEXT NOT NULL,
    texto      TEXT NOT NULL,
    autor      TEXT NOT NULL,
    criado_em  INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    UNIQUE (relatorio, mes, secao)
);

-- Auditoria: toda mudança em expurgo fica registrada
CREATE TABLE IF NOT EXISTS auditoria (
    id        INTEGER PRIMARY KEY,
    tabela    TEXT NOT NULL,
    registro  INTEGER NOT NULL,
    acao      TEXT NOT NULL,
    antes     TEXT,
    depois    TEXT,
    em        INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE TRIGGER IF NOT EXISTS trg_expurgos_upd AFTER UPDATE ON expurgos BEGIN
    INSERT INTO auditoria (tabela, registro, acao, antes, depois) VALUES ('expurgos', OLD.id, 'update',
      json_object('inicio',OLD.inicio,'fim',OLD.fim,'status',OLD.status,'motivo',OLD.motivo,'decidido_por',OLD.decidido_por),
      json_object('inicio',NEW.inicio,'fim',NEW.fim,'status',NEW.status,'motivo',NEW.motivo,'decidido_por',NEW.decidido_por));
END;
CREATE TRIGGER IF NOT EXISTS trg_expurgos_del AFTER DELETE ON expurgos BEGIN
    INSERT INTO auditoria (tabela, registro, acao, antes) VALUES ('expurgos', OLD.id, 'delete',
      json_object('inicio',OLD.inicio,'fim',OLD.fim,'status',OLD.status,'motivo',OLD.motivo));
END;
-- Fechamento não pode ser alterado: revisão = nova versão
CREATE TRIGGER IF NOT EXISTS trg_fechamento_valores_imutavel BEFORE UPDATE ON fechamento_valores BEGIN
    SELECT RAISE(ABORT, 'fechamento é imutável: gere uma nova versão');
END;

-- Chamados do TOPdesk ligados aos incidentes (pelo número) e as ações registradas pelos analistas
CREATE TABLE IF NOT EXISTS chamados (
    numero        TEXT PRIMARY KEY,
    topdesk_id    TEXT,
    titulo        TEXT,
    status        TEXT,
    encerrado     INTEGER NOT NULL DEFAULT 0,
    aberto_em     INTEGER,
    encerrado_em  INTEGER,
    atualizado_em INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE TABLE IF NOT EXISTS chamado_acoes (
    id         TEXT PRIMARY KEY,
    chamado    TEXT NOT NULL REFERENCES chamados(numero),
    clock      INTEGER,
    autor      TEXT,
    analista   INTEGER NOT NULL DEFAULT 1,   -- 1 = registrada por operador; 0 = solicitante/sistema
    texto      TEXT
);
CREATE INDEX IF NOT EXISTS ix_chamado_acoes ON chamado_acoes (chamado, clock);
