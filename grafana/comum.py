import copy, json
base = json.load(open(__import__('os').path.join(__import__('os').path.dirname(__import__('os').path.abspath(__file__)), 'modelo_base_TID.json')))
modelo = base['spec']['elements']['panel-3']
HELPERS = r"""const MESES = ['Janeiro','Fevereiro','Março','Abril','Maio','Junho','Julho',
               'Agosto','Setembro','Outubro','Novembro','Dezembro'];
const META = 99.9;
const num = v => (v === null || v === undefined || v === '' || isNaN(Number(v))) ? null : Number(v);
const fmtPct = n => n === null ? '—' : (n >= 100 ? '100%' :
  (Math.floor(n * 100) / 100).toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + '%');
const fmtDur = s => {
  s = Math.round(Number(s) || 0);
  if (!s) return '—';
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return (d ? d + 'd ' : '') + (d || h ? h + 'h ' : '') + m + 'min';
};
context.handlebars.registerHelper('pct', v => fmtPct(num(v)));
context.handlebars.registerHelper('dur', v => fmtDur(v));
context.handlebars.registerHelper('durmin', v => fmtDur(Number(v || 0) * 60));
context.handlebars.registerHelper('classe', v => {
  const n = num(v);
  return n === null ? 'sem' : n >= 100 ? 'ok' : n >= META ? 'atencao' : 'critico';
});
context.handlebars.registerHelper('mes', v => MESES[Number(v) - 1] || v);
context.handlebars.registerHelper('resumo', data => {
  const linhas = (data || []).filter(l => num(l.DISPONIBILIDADE) !== null);
  const media = linhas.length ? linhas.reduce((a, l) => a + Number(l.DISPONIBILIDADE), 0) / linhas.length : null;
  return {
    media, links: (data || []).length,
    operadoras: new Set((data || []).map(l => l.GRUPO)).size,
    comQueda: linhas.filter(l => Number(l.SEGUNDOS_FORA) > 0).length,
    abaixoMeta: linhas.filter(l => Number(l.DISPONIBILIDADE) < META).length,
    fora: (data || []).reduce((a, l) => a + Number(l.SEGUNDOS_FORA || 0), 0),
    quedas: (data || []).reduce((a, l) => a + Number(l.INCIDENTES || 0), 0),
    mesNome: data && data[0] ? MESES[Number(data[0].MES) - 1] + ' ' + data[0].ANO : '',
  };
});
"""

ESTILO = r"""
.kpis { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 12px; }
@media (max-width: 1000px) { .kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
.kpi { border: 1px solid rgba(128,128,128,.25); border-top: 3px solid #00AAFF; border-radius: 6px; padding: 10px 12px; }
.kpi .t { font-size: 11px; text-transform: uppercase; letter-spacing: .04em; opacity: .7; font-weight: 600; }
.kpi .v { font-size: 28px; font-weight: 700; margin-top: 4px; font-variant-numeric: tabular-nums; }
.kpi .s { font-size: 12px; opacity: .65; margin-top: 2px; }
.ops { display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: 10px; }
.op { border: 1px solid rgba(128,128,128,.25); border-left: 4px solid #73BF69; border-radius: 6px; padding: 8px 10px; }
.op .n { font-weight: 600; font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.op .v { font-size: 22px; font-weight: 700; font-variant-numeric: tabular-nums; }
.op .s { font-size: 11px; opacity: .65; }
.ok .v { color: #73BF69; } .atencao .v { color: #FF9830; } .critico .v { color: #F2495C; } .sem .v { opacity: .45; }
.op.ok { border-left-color: #73BF69; } .op.atencao { border-left-color: #FF9830; } .op.critico { border-left-color: #F2495C; }
.op.sem { border-left-color: rgba(128,128,128,.5); }
.disp { width: 100%; border-collapse: collapse; font-size: 13px; }
.disp th { text-align: left; font-weight: 600; text-transform: uppercase; font-size: 11px; letter-spacing: .04em;
           opacity: .75; padding: 6px 8px; border-bottom: 2px solid #00AAFF; position: sticky; top: 0;
           background: var(--background-primary, #181b1f); }
.disp td { padding: 5px 8px; border-bottom: 1px solid rgba(128,128,128,.18); vertical-align: top; }
.disp tbody tr:nth-child(even) { background: rgba(128,128,128,.06); }
.disp .num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.disp td.num.pct { font-weight: 700; }
.disp .desc { opacity: .7; font-size: 12px; }
.disp tr.ok td.pct { color: #73BF69; } .disp tr.atencao td.pct { color: #FF9830; }
.disp tr.critico td.pct { color: #F2495C; }
.disp tr.atencao td:first-child { box-shadow: inset 3px 0 0 #FF9830; }
.disp tr.critico td:first-child { box-shadow: inset 3px 0 0 #F2495C; }
.aberto { color: #F2495C; font-weight: 600; }
.legenda { font-size: 11px; opacity: .6; margin-top: 8px; }
.vazio { opacity: .6; padding: 8px; }
"""

def painel(pid, titulo, url, conteudo, descricao=""):
    p = copy.deepcopy(modelo)
    s = p["spec"]
    s["id"], s["title"], s["description"] = pid, titulo, descricao
    s["data"]["spec"]["queries"][0]["spec"]["query"]["spec"]["url"] = url
    o = s["vizConfig"]["spec"]["options"]
    o.update(content=conteudo, helpers=HELPERS, styles=ESTILO, renderMode="allRows",
             defaultContent='<div class="vazio">Sem dados para o período selecionado.</div>')
    return p


HELPERS += r"""
context.handlebars.registerHelper('eq', (a, b) => a === b);
context.handlebars.registerHelper('top', (data, n) => (data || []).slice(0, n));
context.handlebars.registerHelper('prefixo', (data, p) => (data || []).filter(l => (l.ITEM || '').toUpperCase().startsWith(p)));
context.handlebars.registerHelper('mediaDisp', rows => {
  const v = (rows || []).map(l => num(l.DISPONIBILIDADE)).filter(x => x !== null);
  return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null;
});
context.handlebars.registerHelper('soma', (rows, campo) => (rows || []).reduce((a, l) => a + Number(l[campo] || 0), 0));
"""
HELPERS = HELPERS.replace("""    quedas: (data || []).reduce((a, l) => a + Number(l.INCIDENTES || 0), 0),""",
  """    quedas: (data || []).reduce((a, l) => a + Number(l.INCIDENTES || 0), 0),
    chamados: (data || []).reduce((a, l) => a + Number(l.CHAMADOS || 0), 0),""")
assert "chamados:" in HELPERS
ESTILO = ESTILO.replace("grid-template-columns: repeat(5, minmax(0, 1fr));", "grid-template-columns: repeat(6, minmax(0, 1fr));")
ESTILO += r"""
.duas-col { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.reg { font-weight: 700; font-size: 14px; margin: 6px 0 4px; }
.reg .v { margin-left: 8px; } .reg.ok .v { color: #73BF69; } .reg.atencao .v { color: #FF9830; } .reg.critico .v { color: #F2495C; }
.disp td.centro, .disp th.centro { text-align: center; }
.disp tr.exp td { opacity: .55; } .selo { font-size: 10px; padding: 1px 5px; border-radius: 3px; border: 1px solid rgba(128,128,128,.5); white-space: nowrap; }
"""

API = "__API_URL__"   # marcador: o instalar.py troca pelo endereço real, a partir de SERVIDOR_IP no env
PER = "de=${__from:date:YYYY-MM-DD}&ate=${__to:date:YYYY-MM-DD}"
VAZIO = '<div class="vazio">Sem dados para o período selecionado.</div>'

def item(nome, x, y, w, h):
    return {"kind": "GridLayoutItem", "spec": {"element": {"kind": "ElementReference", "name": nome},
            "x": x, "y": y, "width": w, "height": h}}

def resumo_kpis(titulo_geral, nome_itens, extra_s=""):
    return ("""{{#with (resumo data)}}
<div class="kpis">
  <div class="kpi {{classe media}}"><div class="t">""" + titulo_geral + """</div><div class="v">{{pct media}}</div><div class="s">média de {{links}} """ + nome_itens + """</div></div>
  <div class="kpi"><div class="t">""" + nome_itens.capitalize() + """ monitorados</div><div class="v">{{links}}</div><div class="s">""" + extra_s + """</div></div>
  <div class="kpi"><div class="t">Com queda</div><div class="v">{{comQueda}}</div><div class="s">{{quedas}} quedas no período</div></div>
  <div class="kpi {{#if abaixoMeta}}critico{{else}}ok{{/if}}"><div class="t">Abaixo da meta (99,90%)</div><div class="v">{{abaixoMeta}}</div><div class="s">""" + nome_itens + """</div></div>
  <div class="kpi"><div class="t">Tempo total fora</div><div class="v">{{dur fora}}</div><div class="s">soma de todos</div></div>
  <div class="kpi"><div class="t">Chamados</div><div class="v">{{chamados}}</div><div class="s">abertos para as quedas</div></div>
</div>
{{/with}}""")

def tabela(rotulo, extra_col=None, extra_campo=None, fonte="data"):
    cab_extra = f"<th>{extra_col}</th>" if extra_col else ""
    td_extra = f'<td class="desc">{{{{{extra_campo}}}}}</td>' if extra_campo else ""
    return (f"""<table class="disp">
  <thead><tr><th>{rotulo}</th>{cab_extra}<th class="num">Disponibilidade</th><th class="num">Tempo fora</th><th class="num">Quedas</th><th class="num">Chamados</th></tr></thead>
  <tbody>
  {{{{#each {fonte}}}}}
    <tr class="{{{{classe DISPONIBILIDADE}}}}">
      <td>{{{{NAME}}}}</td>{td_extra}
      <td class="num pct">{{{{pct DISPONIBILIDADE}}}}</td><td class="num">{{{{dur SEGUNDOS_FORA}}}}</td><td class="num">{{{{INCIDENTES}}}}</td><td class="num">{{{{CHAMADOS}}}}</td>
    </tr>
  {{{{/each}}}}
  </tbody>
</table>""")

QUEDAS = """<table class="disp">
  <thead><tr><th>Item</th><th>Grupo</th><th>Início</th><th>Fim</th><th class="num">Duração</th><th>Chamado</th></tr></thead>
  <tbody>
  {{#each data}}
    <tr class="{{#if EXPURGO}}exp{{/if}}" title="{{MOTIVO_EXPURGO}}">
      <td>{{ITEM}}{{#if EXPURGO}} <span class="selo">{{#if (eq EXPURGO "total")}}não contabilizado{{else}}parcialmente não contabilizado{{/if}}</span>{{/if}}</td><td>{{GRUPO}}</td><td class="num">{{INICIO}}</td>
      <td class="num">{{#if FIM}}{{FIM}}{{else}}<span class="aberto">em aberto</span>{{/if}}</td>
      <td class="num">{{durmin MINUTOS}}</td><td>{{CHAMADO}}</td>
    </tr>
  {{/each}}
  </tbody>
</table>"""

def painel(pid, titulo, url, conteudo, descricao=""):
    p = copy.deepcopy(modelo)
    s = p["spec"]
    s["id"], s["title"], s["description"] = pid, titulo, descricao
    s["data"]["spec"]["queries"][0]["spec"]["query"]["spec"]["url"] = url
    o = s["vizConfig"]["spec"]["options"]
    o.update(content=conteudo, helpers=HELPERS, styles=ESTILO, renderMode="allRows", defaultContent=VAZIO)
    return p

def dashboard(uid, titulo, descricao, tags, elementos, layout, variaveis):
    d = copy.deepcopy(base)
    d["metadata"] = {"name": uid}
    s = d["spec"]
    s.update(title=titulo, description=descricao, tags=tags, elements=elementos, variables=variaveis)
    s["layout"]["spec"]["items"] = layout
    s["timeSettings"].update({"from": "now/M", "to": "now/M", "timezone": "America/Sao_Paulo"})
    return json.loads(json.dumps(d).replace("__INFINITY_UID__", "dg2gfvwh9s0sgb"))

def var_cliente():
    return copy.deepcopy(base["spec"]["variables"][0])
