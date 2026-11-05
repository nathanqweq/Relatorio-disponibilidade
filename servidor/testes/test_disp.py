"""Testes do motor de disponibilidade.  Rodar: python3 -m pytest -q testes/test_disp.py"""
import sqlite3
import pytest

import disp

MES = "2026-07"
INI, FIM = disp.limites_mes(MES)          # julho/2026 em Brasília
H = 3600
TOTAL = FIM - INI                         # 31 dias


@pytest.fixture
def conn(tmp_path):
    c = disp.conectar(str(tmp_path / "t.db"))
    c.execute("INSERT INTO objetos (id, relatorio, item, grupo, nome, ativo_desde) VALUES "
              "(1,'links','UHSA_OI','OI','UHSA_OI',0),(2,'links','GAPG_OI','OI','GAPG_OI',0),"
              "(3,'links','CESA_EMB','EMBRATEL','CESA_EMB',0)")
    return c


def inc(c, obj, ini, fim, ev):
    c.execute("INSERT INTO incidentes (objeto_id, zabbix_eventid, inicio, fim) VALUES (?,?,?,?)", (obj, ev, ini, fim))


def por_item(linhas):
    return {l["item"]: l for l in linhas}


def test_intervalos():
    assert disp.unir([(5, 8), (1, 3), (2, 4), (8, 9)]) == [(1, 4), (5, 9)]
    assert disp.intersectar([(0, 10)], [(2, 3), (8, 12)]) == [(2, 3), (8, 10)]
    assert disp.subtrair([(0, 10)], [(2, 3), (8, 12)]) == [(0, 2), (3, 8)]


def test_mes_fuso_brasilia():
    assert TOTAL == 31 * 86400
    assert disp.epoch("2026-07-01") == INI   # 01/07 00:00 em Brasília = 03:00 UTC


def test_sem_incidente_100(conn):
    l = por_item(disp.calcular(conn, "links", MES, agora=FIM + 10))
    assert l["CESA_EMB"]["disponibilidade"] == 1.0


def test_sobreposicao_conta_uma_vez(conn):
    inc(conn, 1, INI + 10 * H, INI + 12 * H, "a")
    inc(conn, 1, INI + 11 * H, INI + 13 * H, "b")       # sobrepõe 1h
    l = por_item(disp.calcular(conn, "links", MES, agora=FIM + 10))["UHSA_OI"]
    assert l["segundos_fora"] == 3 * H
    assert l["disponibilidade"] == pytest.approx(1 - 3 * H / TOTAL)


def test_virada_de_mes_e_aberto(conn):
    inc(conn, 1, INI - 5 * H, INI + 2 * H, "a")         # começou em junho
    inc(conn, 2, FIM - 1 * H, None, "b")                # ainda aberto
    l = por_item(disp.calcular(conn, "links", MES, agora=FIM + 99 * H))
    assert l["UHSA_OI"]["segundos_fora"] == 2 * H
    assert l["GAPG_OI"]["segundos_fora"] == 1 * H


def test_mes_corrente_ate_agora(conn):
    agora = INI + 10 * 86400
    inc(conn, 1, INI + H, None, "a")
    l = por_item(disp.calcular(conn, "links", MES, agora=agora))["UHSA_OI"]
    assert l["segundos_periodo"] == 10 * 86400
    assert l["segundos_fora"] == 10 * 86400 - H


def test_expurgo_so_aprovado_e_so_dentro_do_incidente(conn):
    inc(conn, 1, INI + 10 * H, INI + 14 * H, "a")       # 4h fora
    for st in ("aprovado", "proposto", "rejeitado"):
        conn.execute("INSERT INTO expurgos (objeto_id, inicio, fim, tipo, motivo, proposto_por, status) "
                     "VALUES (1,?,?,'outro','x','t',?)", (INI + 12 * H, INI + 20 * H, st))
    l = por_item(disp.calcular(conn, "links", MES, agora=FIM + 10))["UHSA_OI"]
    assert l["segundos_expurgo"] == 2 * H              # só 12h-14h, e só o aprovado
    assert l["disponibilidade"] == pytest.approx(1 - 2 * H / TOTAL)


def test_expurgo_do_relatorio_inteiro(conn):
    inc(conn, 1, INI + H, INI + 2 * H, "a")
    inc(conn, 3, INI + H, INI + 2 * H, "b")
    conn.execute("INSERT INTO expurgos (relatorio, inicio, fim, tipo, motivo, proposto_por, status) "
                 "VALUES ('links',?,?,'manutencao_programada','janela','t','aprovado')", (INI, INI + 3 * H))
    for l in disp.calcular(conn, "links", MES, agora=FIM + 10):
        assert l["disponibilidade"] == 1.0


def test_lacuna_sai_do_calculo(conn):
    inc(conn, 1, INI + 10 * H, INI + 14 * H, "a")
    conn.execute("INSERT INTO lacunas (inicio, fim, motivo, criado_por) VALUES (?,?,'zabbix fora','t')",
                 (INI + 12 * H, INI + 16 * H))
    l = por_item(disp.calcular(conn, "links", MES, agora=FIM + 10))["UHSA_OI"]
    assert l["segundos_lacuna"] == 4 * H
    assert l["segundos_fora"] == 2 * H
    assert l["disponibilidade"] == pytest.approx(1 - 2 * H / (TOTAL - 4 * H))


def test_objeto_novo_no_meio_do_mes(conn):
    conn.execute("UPDATE objetos SET ativo_desde=? WHERE id=3", (INI + 21 * 86400,))
    inc(conn, 3, INI + 25 * 86400, INI + 25 * 86400 + H, "a")
    l = por_item(disp.calcular(conn, "links", MES, agora=FIM + 10))["CESA_EMB"]
    assert l["segundos_periodo"] == 10 * 86400
    assert l["disponibilidade"] == pytest.approx(1 - H / (10 * 86400))


def test_agregacao_por_grupo(conn):
    inc(conn, 1, INI, INI + TOTAL // 10, "a")          # UHSA 90%
    g = disp.agregar(disp.calcular(conn, "links", MES, agora=FIM + 10))
    assert g["OI"] == pytest.approx(0.95)               # média de 90% e 100%
    assert g["EMBRATEL"] == 1.0


def preparar_remocao(c):
    """Item 1 encerrado, com tudo ligado a ele; item 2 divide um chamado com ele."""
    c.execute("UPDATE objetos SET ativo_ate=? WHERE id=1", (FIM,))
    c.execute("INSERT INTO objeto_triggers (objeto_id, triggerid) VALUES (1,'t1'),(2,'t2')")
    c.execute("INSERT INTO incidentes (id, objeto_id, zabbix_eventid, inicio, fim, chamado) VALUES "
              "(1,1,'a',?,?,'SO_DELE'),(2,1,'b',?,?,'DIVIDIDO'),(3,2,'c',?,?,'DIVIDIDO')",
              (INI + H, INI + 2 * H, INI + 3 * H, INI + 4 * H, INI + 5 * H, INI + 6 * H))
    c.execute("INSERT INTO comentarios (incidente_id, clock, mensagem) VALUES (1,1,'x'),(3,1,'y')")
    c.execute("INSERT INTO chamados (numero) VALUES ('SO_DELE'),('DIVIDIDO')")
    c.execute("INSERT INTO chamado_acoes (id, chamado, texto) VALUES ('a1','SO_DELE','t'),('a2','DIVIDIDO','t')")
    for ob, rel, inc in ((1, None, 1), (None, "links", 2), (2, None, 3)):        # do item, da medição, de outro item
        c.execute("INSERT INTO expurgos (objeto_id, relatorio, incidente_id, inicio, fim, tipo, motivo, proposto_por, status) "
                  "VALUES (?,?,?,?,?,'outro','m','p','aprovado')", (ob, rel, inc, INI, INI + H))
    c.commit()


def conta(c, tabela):
    return c.execute(f"SELECT count(*) FROM {tabela}").fetchone()[0]


def test_remover_item_apaga_so_o_que_e_dele(conn):
    preparar_remocao(conn)
    d = disp.contagem_item(conn, 1)
    assert (d["incidentes"], d["comentarios"], d["expurgos"], d["chamados"]) == (2, 1, 1, 2)
    disp.remover_item(conn, 1, "ana")
    assert [r[0] for r in conn.execute("SELECT id FROM objetos ORDER BY 1")] == [2, 3]
    assert [r[0] for r in conn.execute("SELECT id FROM incidentes")] == [3]
    assert [r[0] for r in conn.execute("SELECT triggerid FROM objeto_triggers")] == ["t2"]   # não importa mais os eventos
    assert [r[0] for r in conn.execute("SELECT mensagem FROM comentarios")] == ["y"]
    assert [r[0] for r in conn.execute("SELECT numero FROM chamados")] == ["DIVIDIDO"]       # o que outro item usa fica
    assert [r[0] for r in conn.execute("SELECT id FROM chamado_acoes")] == ["a2"]
    exp = conn.execute("SELECT objeto_id, relatorio, incidente_id FROM expurgos ORDER BY id").fetchall()
    assert [tuple(e) for e in exp] == [(None, "links", None), (2, None, 3)]                  # o da medição inteira fica
    aud = conn.execute("SELECT * FROM auditoria WHERE tabela='objetos'").fetchone()
    assert aud["acao"] == "delete" and "UHSA_OI" in aud["antes"] and "ana" in aud["depois"]
    assert "UHSA_OI" not in por_item(disp.calcular(conn, "links", MES, agora=FIM + 10))


def test_remover_item_ativo_e_recusado(conn):
    with pytest.raises(SystemExit, match="ainda está ativo"):
        disp.remover_item(conn, 2, "ana")
    assert conta(conn, "objetos") == 3
    with pytest.raises(SystemExit, match="não encontrado"):
        disp.remover_item(conn, 99, "ana")


def test_remover_item_de_mes_fechado_exige_confirmacao(conn):
    preparar_remocao(conn)
    conn.execute("INSERT INTO fechamentos (id, relatorio, mes, versao, gerado_por) VALUES (1,'links',?,1,'s')", (MES,))
    conn.execute("INSERT INTO fechamento_valores (fechamento_id, objeto_id, segundos_periodo, segundos_fora, segundos_expurgo) VALUES (1,1,1,0,0),(1,2,1,0,0)")
    conn.commit()
    with pytest.raises(SystemExit, match=MES):
        disp.remover_item(conn, 1, "ana")
    assert conta(conn, "incidentes") == 3 and conta(conn, "objetos") == 3        # recusou sem apagar nada
    disp.remover_item(conn, 1, "ana", incluir_fechados=True)
    assert conta(conn, "fechamento_valores") == 1 and conta(conn, "fechamentos") == 1


def test_gravar_incidente_idempotente(conn):
    for _ in range(2):
        disp.gravar_incidente(conn, "links", "UHSA_OI", "900", None, INI + H, None, 4, "down")
    disp.gravar_incidente(conn, "links", "UHSA_OI", "900", "901", INI + H, INI + 2 * H, 4, "down",
                          chamado="I2607-123", chamado_externo="OI-4337964")
    r = conn.execute("SELECT count(*) n, fim, chamado, chamado_externo FROM incidentes").fetchone()
    assert (r["n"], r["fim"], r["chamado"], r["chamado_externo"]) == (1, INI + 2 * H, "I2607-123", "OI-4337964")


def test_auditoria_e_fechamento_imutavel(conn):
    conn.execute("INSERT INTO expurgos (id, objeto_id, inicio, fim, tipo, motivo, proposto_por) "
                 "VALUES (7,1,?,?,'outro','x','t')", (INI, INI + H))
    conn.execute("UPDATE expurgos SET status='aprovado', decidido_por='gestor' WHERE id=7")
    assert conn.execute("SELECT count(*) FROM auditoria WHERE registro=7").fetchone()[0] == 1
    conn.execute("INSERT INTO fechamentos (id, relatorio, mes, versao, gerado_por) VALUES (1,'links',?,1,'t')", (MES,))
    conn.execute("INSERT INTO fechamento_valores VALUES (1,1,100,0,0,1.0)")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE fechamento_valores SET disponibilidade=0.5")


def test_pct_corta_em_vez_de_arredondar():
    assert disp.pct(1.0) == "100,00%"
    assert disp.pct(0.99997) == "99,99%"
    assert disp.pct(0.9969) == "99,69%"
    assert disp.pct(None) == "—"


def test_extrair_chamado():
    ack = lambda m, c=1: {"message": m, "clock": str(c)}
    assert disp.extrair_chamado([ack("Chamado aberto no TOPdesk: I2610-123")]) == "I2610-123"
    assert disp.extrair_chamado([ack("Ticket #4337964 criado")]) == "4337964"
    assert disp.extrair_chamado([ack("Incidente nº I 2610 045 registrado")]) == "I 2610 045"
    assert disp.extrair_chamado([ack("Aberto I2610-777 via integração")]) == "I2610-777"
    assert disp.extrair_chamado([ack("sem número aqui")]) is None
    assert disp.extrair_chamado([ack("Chamado: I2610-2", 9), ack("Chamado: I2610-1", 1)]) == "I2610-1"
    assert disp.extrair_chamado(None) is None
    assert disp.extrair_chamado([ack("Chamado encerrado: I2610-12878", 2), ack("Chamado criado: I2610-12878", 1)]) == "I2610-12878"


def test_comentarios_sem_duplicar(conn):
    iid = disp.gravar_incidente(conn, "links", "UHSA_OI", "500", None, INI, INI + H, 4, "down")
    acks = [{"clock": "10", "username": "godesk-user", "message": "Chamado criado: I2610-12878"},
            {"clock": "20", "username": "godesk-user", "message": "Chamado encerrado: I2610-12878"}]
    disp.gravar_comentarios(conn, iid, acks)
    disp.gravar_comentarios(conn, iid, acks)
    rows = conn.execute("SELECT usuario, mensagem FROM comentarios ORDER BY clock").fetchall()
    assert [tuple(r) for r in rows] == [("godesk-user", "Chamado criado: I2610-12878"),
                                        ("godesk-user", "Chamado encerrado: I2610-12878")]


def test_limpar_mantem_mes_atual_e_tres_anteriores(conn):
    agora = disp.epoch("2026-11-15")
    for ev, mes in (("a", "2026-07-10"), ("b", "2026-08-10"), ("c", "2026-11-02")):
        ini = disp.epoch(mes)
        iid = disp.gravar_incidente(conn, "links", "UHSA_OI", ev, None, ini, ini + H, 4, "down")
        disp.gravar_comentarios(conn, iid, [{"clock": str(ini), "message": "Chamado criado: I2607-1"}])
    aberto = disp.gravar_incidente(conn, "links", "GAPG_OI", "z", None, disp.epoch("2026-06-01"), None, 4, "down")
    conn.execute("INSERT INTO expurgos (objeto_id, incidente_id, inicio, fim, tipo, motivo, proposto_por) "
                 "VALUES (1, 1, ?, ?, 'outro', 'x', 't')", (disp.epoch("2026-07-10"), disp.epoch("2026-07-11")))
    conn.execute("INSERT INTO fechamentos (relatorio, mes, versao, gerado_por) VALUES ('links','2026-06',1,'t')")
    corte, n = disp.limpar(conn, 3, agora=agora)
    assert corte == "2026-08"
    restantes = {r[0] for r in conn.execute("SELECT zabbix_eventid FROM incidentes")}
    assert restantes == {"b:links", "c:links", "z:links"}       # julho saiu; aberto fica
    assert n["incidentes"] == 1 and n["expurgos"] == 1 and n["comentarios"] == 1
    assert conn.execute("SELECT count(*) FROM fechamentos").fetchone()[0] == 1
    disp.limpar(conn, 3, agora=agora, incluir_fechamentos=True)
    assert conn.execute("SELECT count(*) FROM fechamentos").fetchone()[0] == 0
    assert disp.limpar(conn, 3, agora=disp.epoch("2026-02-10"))[0] == "2025-11"


def test_grupo_vem_da_tag_do_host_quando_trigger_nao_tem():
    host = {"name": "LINK_GAPG_ALGAR", "host": "x", "tags": [{"tag": "grupo", "value": "ALGAR"}]}
    trig = [{"tag": "relatorio", "value": "links-internet"}, {"tag": "item", "value": "{HOST.NAME}"}]
    assert disp.chaves_objeto(trig, host)[0] == [("links-internet", "LINK_GAPG_ALGAR", "ALGAR")]
    trig2 = trig + [{"tag": "grupo", "value": "OI"}]          # tag da trigger tem prioridade
    assert disp.chaves_objeto(trig2, host)[0] == [("links-internet", "LINK_GAPG_ALGAR", "OI")]
    assert disp.chaves_objeto(trig, {"name": "Y", "host": "y"})[0] == [("links-internet", "Y", None)]


def test_macro_de_usuario_com_heranca():
    class Falso:
        def call(self, metodo, p):
            return {
                "usermacro.get": [{"macro": "{$LINK.OPERADORA}", "value": "GLOBAL"}, {"macro": "{$X}", "value": "g"}],
                "host.get": [{"hostid": "1", "macros": [], "parentTemplates": [{"templateid": "10"}]},
                             {"hostid": "2", "macros": [{"macro": "{$LINK.OPERADORA}", "value": "OI"}],
                              "parentTemplates": [{"templateid": "10"}]}],
                "template.get": [{"templateid": "10", "macros": [{"macro": "{$LINK.OPERADORA}", "value": "ALGAR"}],
                                  "parentTemplates": [{"templateid": "20"}]},
                                 {"templateid": "20", "macros": [{"macro": "{$X}", "value": "t20"}],
                                  "parentTemplates": []}],
            }[metodo]
    m = disp.macros_resolvidas(Falso(), ["1", "2"])
    assert m["1"]["{$LINK.OPERADORA}"] == "ALGAR" and m["1"]["{$X}"] == "t20"   # template > global
    assert m["2"]["{$LINK.OPERADORA}"] == "OI"                                    # host > template
    host = {"name": "L1", "host": "l1", "macros": m["2"]}
    trig = [{"tag": "relatorio", "value": "links-internet"}, {"tag": "grupo", "value": "{$LINK.OPERADORA}"}]
    assert disp.chaves_objeto(trig, host)[0] == [("links-internet", "L1", "OI")]
    assert disp.expandir("{$NAO.EXISTE}", host) == "{$NAO.EXISTE}"


def test_descricao_pelas_tags_do_link():
    tags = [{"tag": "DESIGNACAO", "value": "CTA 0829811"}, {"tag": "VELOCIDADE", "value": "150"},
            {"tag": "LOCALIDADE", "value": "Rifaina/SP"}]
    assert disp.descricao_por_tags(tags) == "CTA 0829811 · 150 Mbps · Rifaina/SP"
    assert disp.descricao_por_tags([{"tag": "AMBIENTE", "value": "PRD"}]) is None


def test_objeto_removido_do_zabbix_sai_dos_meses_seguintes(conn):
    saiu = disp.desativar_ausentes(conn, vistos={1, 2}, agora=INI + 10 * 86400)    # CESA_EMB (3) sumiu dia 11/07
    assert saiu == [("links", "CESA_EMB")]
    jul = por_item(disp.calcular(conn, "links", MES, agora=FIM + 10))
    assert jul["CESA_EMB"]["segundos_periodo"] == 10 * 86400                     # julho: conta até sair
    ago = por_item(disp.calcular(conn, "links", "2026-08", agora=FIM + 40 * 86400))
    assert "CESA_EMB" not in ago and "UHSA_OI" in ago                            # agosto: não aparece
    assert disp.desativar_ausentes(conn, vistos={1, 2}) == []                    # não encerra de novo


def test_chamado_topdesk_e_acao_do_analista(conn):
    inc = {"id": "abc", "briefDescription": "DISASTER - FW DOWN", "processingStatus": {"name": "Fechado"},
           "closed": True, "callDate": "2026-07-29T12:28:23.000+0000", "closedDate": "2026-07-29T13:00:00.000+0000"}
    acoes = [
        {"id": "1", "entryDate": "2026-07-29T12:30:00.000+0000", "operator": {"name": "Ana"},
         "memoText": "Verificado link.<br>Acionada operadora."},
        {"id": "2", "entryDate": "2026-07-29T12:50:00.000+0000", "person": {"name": "Cliente"}, "memoText": "ok"},
        {"id": "3", "entryDate": "2026-07-29T12:55:00.000+0000", "operator": {"name": "Ana"},
         "plainText": "Normalizado antes do contato com a equipe."},
    ]
    disp.gravar_chamado(conn, "I2607-1", inc, acoes)
    disp.gravar_chamado(conn, "I2607-1", inc, acoes)                 # sem duplicar
    assert conn.execute("SELECT count(*) FROM chamado_acoes").fetchone()[0] == 3
    c = conn.execute("SELECT encerrado, status, aberto_em FROM chamados").fetchone()
    assert (c["encerrado"], c["status"]) == (1, "Fechado") and c["aberto_em"] == 1785328103
    assert disp.acao_principal(conn, "I2607-1") == "Normalizado antes do contato com a equipe."
    assert conn.execute("SELECT texto FROM chamado_acoes WHERE id='1'").fetchone()[0] == "Verificado link.\nAcionada operadora."


def test_periodo_livre(conn):
    ini, fim = disp.limites_mes("2026-07-26..2026-07-30")
    assert (ini, fim - ini) == (disp.epoch("2026-07-26"), 5 * 86400)
    inc(conn, 1, disp.epoch("2026-07-25 12:00"), disp.epoch("2026-07-26 06:00"), "a")   # começa antes
    inc(conn, 1, disp.epoch("2026-07-29 10:00"), disp.epoch("2026-07-29 11:00"), "b")
    l = por_item(disp.calcular(conn, "links", "2026-07-26..2026-07-30", agora=FIM + 10))["UHSA_OI"]
    assert l["segundos_periodo"] == 5 * 86400 and l["segundos_fora"] == 7 * H


def test_queda_expurgada_inteira_some_e_parcial_fica(conn):
    inc(conn, 1, INI + 10 * H, INI + 12 * H, "a")      # 2h — vai ser expurgada inteira (falso positivo)
    inc(conn, 1, INI + 20 * H, INI + 24 * H, "b")      # 4h — expurgo só de 1h
    ia, ib = [r[0] for r in conn.execute("SELECT id FROM incidentes ORDER BY inicio")]
    conn.execute("INSERT INTO expurgos (objeto_id, incidente_id, inicio, fim, tipo, motivo, proposto_por, status) "
                 "VALUES (1,?,?,?,'falso_positivo','alarme falso','t','aprovado')", (ia, INI + 10 * H, INI + 12 * H))
    conn.execute("INSERT INTO expurgos (objeto_id, incidente_id, inicio, fim, tipo, motivo, proposto_por, status) "
                 "VALUES (1,?,?,?,'manutencao_programada','janela','t','aprovado')", (ib, INI + 20 * H, INI + 21 * H))
    assert disp.situacao_expurgo(conn, ia) == ("total", "alarme falso")
    assert disp.situacao_expurgo(conn, ib) == ("parcial", "janela")
    assert disp.descricao_expurgo(conn, ia) == "Falso positivo: alarme falso (por t)"
    l = por_item(disp.calcular(conn, "links", MES, agora=FIM + 10))["UHSA_OI"]
    assert l["incidentes"] == 1 and l["segundos_fora"] - l["segundos_expurgo"] == 3 * H


def test_desconsiderar_pelo_chamado_sem_aprovacao(conn):
    i1 = disp.gravar_incidente(conn, "links", "UHSA_OI", "e1", None, INI + 10 * H, INI + 12 * H, 4, "down", chamado="I2607-9")
    i2 = disp.gravar_incidente(conn, "links", "UHSA_OI", "e2", None, INI + 30 * H, INI + 31 * H, 4, "down", chamado="I2607-9")
    ids = [r[0] for r in conn.execute("SELECT id FROM incidentes WHERE chamado='I2607-9'")]
    criados = disp.desconsiderar(conn, ids, "falso_positivo", "alarme falso do link", "ana")
    assert len(criados) == 2 and disp.desconsiderar(conn, ids, "falso_positivo", "x", "ana") == []   # não duplica
    l = por_item(disp.calcular(conn, "links", MES, agora=FIM + 10))["UHSA_OI"]
    assert l["disponibilidade"] == 1.0 and l["segundos_fora"] == 3 * H and l["segundos_expurgo"] == 3 * H
    assert disp.situacao_expurgo(conn, i1)[0] == "total"
    conn.execute("UPDATE expurgos SET status='rejeitado' WHERE incidente_id=?", (i2,))      # "contabilizar"
    l = por_item(disp.calcular(conn, "links", MES, agora=FIM + 10))["UHSA_OI"]
    assert l["segundos_fora"] - l["segundos_expurgo"] == 1 * H
