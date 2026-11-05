"use strict";
// Painel de administração. Sem dependências. Todo texto entra no DOM como texto (nunca innerHTML):
// nomes de problema e ações de chamado vêm de sistemas externos.

const $app = document.getElementById("app");
let LISTAS = null;
const filtros = {};
let renderAtual = 0;

function h(tag, props, ...filhos) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "classe") el.className = v;
    else if (k in el && k !== "list" && k !== "form") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const f of filhos.flat(Infinity)) {
    if (f === null || f === undefined || f === false) continue;
    el.append(f.nodeType ? f : String(f));
  }
  return el;
}

class ErroApi extends Error {}

async function api(caminho, corpo) {
  const opcoes = corpo === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json", "X-Painel": "1" }, body: JSON.stringify(corpo),
  };
  let r;
  try { r = await fetch("/api" + caminho, opcoes); }
  catch { throw new ErroApi("Sem conexão com o servidor do painel."); }
  const d = await r.json().catch(() => ({}));
  if (r.status === 401 && caminho !== "/login") { telaLogin(d.erro); throw new ErroApi(d.erro || "Sessão expirada"); }
  if (!r.ok) throw new ErroApi(d.erro || `Erro ${r.status}`);
  return d;
}

const consulta = (o) => {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(o)) if (v !== "" && v !== null && v !== undefined) p.set(k, v);
  const s = p.toString();
  return s ? "?" + s : "";
};

function toast(texto, tipo) {
  const el = h("div", { classe: "toast " + (tipo || "") }, texto);
  document.getElementById("avisos").append(el);
  setTimeout(() => el.remove(), tipo ? 9000 : 4000);
}

function resultado(r) {
  if (r && r.mensagem) toast(r.mensagem);
  if (r && r.aviso) toast(r.aviso, "aviso");
}

async function acao(fn, depois) {
  try {
    resultado(await fn());
    if (depois) await depois();
  } catch (e) { toast(e.message, "erro"); }
}

// ------------------------------------------------------- componentes ----

function tabela(colunas, linhas, opc = {}) {
  if (!linhas.length) return h("div", { classe: "vazio" }, opc.vazio || "Nada para mostrar.");
  return h("div", { classe: "tabela-rolagem" }, h("table", { classe: "t" },
    h("thead", {}, h("tr", {}, colunas.map((c) => h("th", { classe: c.classe }, c.titulo)))),
    h("tbody", {}, linhas.map((l) => h("tr", {
      classe: opc.aoClicar ? "clicavel" : null,
      onclick: opc.aoClicar ? () => opc.aoClicar(l) : null,
    }, colunas.map((c) => h("td", { classe: c.classe }, c.valor(l))))))));
}

const selo = (texto, tipo) => h("span", { classe: "selo " + (tipo || "") }, texto);
const botao = (texto, onclick, classe) => h("button", { type: "button", classe, onclick }, texto);
const pequeno = (texto, onclick, extra) => botao(texto, (ev) => { ev.stopPropagation(); onclick(ev); }, "pequeno " + (extra || ""));

function campoEntrada(c) {
  if (c.tipo === "select") {
    return h("select", { name: c.nome, required: c.obrigatorio },
      c.opcoes.map((o) => h("option", { value: o.valor, selected: String(c.valor ?? "") === String(o.valor) }, o.rotulo)));
  }
  if (c.tipo === "area") return h("textarea", { name: c.nome, required: c.obrigatorio, rows: c.linhas || 3, classe: c.classe, value: c.valor ?? "", spellcheck: c.classe ? false : null });
  return h("input", {
    name: c.nome, type: c.tipo || "text", required: c.obrigatorio, value: c.valor ?? "", placeholder: c.placeholder,
    min: c.min, max: c.max, step: c.step, autocomplete: c.autocomplete || "off",
  });
}

function campo(c) {
  if (c.tipo === "marcar") {
    return h("label", { classe: "marcar" }, h("input", { type: "checkbox", name: c.nome, checked: c.valor }), c.rotulo);
  }
  return h("label", { classe: "campo" }, c.rotulo, campoEntrada(c), c.ajuda ? h("span", { classe: "ajuda" }, c.ajuda) : null);
}

function lerForm(form) {
  const d = {};
  for (const el of form.elements) {
    if (!el.name || (el.type === "radio" && !el.checked)) continue;
    d[el.name] = el.type === "checkbox" ? el.checked : el.value;
  }
  return d;
}

function dialogo(conteudo, largo) {
  const d = h("dialog", { classe: largo ? "largo" : null }, conteudo);
  d.addEventListener("close", () => d.remove());
  document.body.append(d);
  d.showModal();
  return d;
}

// Formulário em janela: o erro aparece na própria janela, sem perder o que foi digitado.
function abrirForm({ titulo, nota, conteudo, campos, botaoTexto, perigo, enviar, largo, extras }) {
  const erro = h("div", { classe: "erro-form", hidden: true });
  const salvar = h("button", { type: "submit", classe: perigo ? "perigo" : "primario" }, botaoTexto || "Salvar");
  const form = h("form", {
    classe: "formulario", onsubmit: async (ev) => {
      ev.preventDefault();
      erro.hidden = true;
      salvar.disabled = true;
      try {
        resultado(await enviar(lerForm(form)));
        d.close();
        rerender();
      } catch (e) {
        erro.textContent = e.message;
        erro.hidden = false;
        erro.scrollIntoView({ block: "nearest" });
      } finally { salvar.disabled = false; }
    },
  },
  h("h2", {}, titulo),
  nota ? h("p", { classe: "nota" }, nota) : null,
  conteudo,
  campos.map((c) => (Array.isArray(c) ? h("div", { classe: "duas" }, c.map(campo)) : campo(c))),
  erro,
  h("div", { classe: "rodape-dialogo" }, extras ? extras(() => d.close()) : null,
    h("button", { type: "button", onclick: () => d.close() }, "Cancelar"), salvar));
  const d = dialogo(form, largo);
  return d;
}

function confirmar({ titulo, texto, botaoTexto, enviar, perigo = true, depois }) {
  return abrirForm({
    titulo, nota: texto, campos: [], botaoTexto, perigo,
    enviar: async () => { const r = await enviar(); if (depois) depois(r); return r; },
  });
}

const opcoesMedicao = (comTodas, rotuloTodas) => [
  ...(comTodas ? [{ valor: "", rotulo: rotuloTodas || "Todas" }] : []),
  ...LISTAS.medicoes.map((m) => ({ valor: m, rotulo: m })),
];
const opcoesPdf = (comTodos) => [
  ...(comTodos ? [{ valor: "", rotulo: "Todos os relatórios" }] : []),
  ...LISTAS.relatorios_pdf.map((r) => ({ valor: r.id, rotulo: `${r.id} - ${r.titulo}` })),
];
const opcoesTipo = () => Object.entries(LISTAS.tipos_expurgo).map(([valor, rotulo]) => ({ valor, rotulo }));

function barraFiltros(nome, campos, aoFiltrar) {
  const estado = (filtros[nome] = filtros[nome] || {});
  for (const c of campos) if (estado[c.nome] === undefined) estado[c.nome] = c.valor ?? "";
  const els = campos.map((c) => {
    const el = campoEntrada({ ...c, obrigatorio: false, valor: estado[c.nome] });
    el.addEventListener("change", () => { estado[c.nome] = el.value; aoFiltrar(); });
    return h("label", { classe: "campo" }, c.rotulo, el);
  });
  return { el: h("div", { classe: "filtros" }, els), estado };
}

function cabecalho(titulo, ...acoes) {
  return h("div", { classe: "cabeca" }, h("h1", {}, titulo), h("div", { classe: "acoes" }, acoes));
}

const tamanho = (n) => {
  if (n >= 1e9) return (n / 1e9).toFixed(1).replace(".", ",") + " GB";
  if (n >= 1e6) return (n / 1e6).toFixed(1).replace(".", ",") + " MB";
  return Math.ceil(n / 1024) + " KB";
};

// ---------------------------------------------------------- tarefas ----

async function iniciarTarefa(tipo, parametros) {
  try {
    const t = await api("/tarefas", { tipo, parametros: parametros || {} });
    abrirTarefa(t.id, t.titulo);
  } catch (e) { toast(e.message, "erro"); }
}

function seloTarefa(estado) {
  const m = { executando: ["executando", "info"], concluida: ["concluída", "ok"], falhou: ["falhou", "erro"], cancelada: ["cancelada", "aviso"] };
  return selo(...(m[estado] || [estado]));
}

function abrirTarefa(id, titulo) {
  const saida = h("pre", { classe: "saida" }, "");
  const estado = h("span", {}, seloTarefa("executando"));
  const cancelar = botao("Cancelar tarefa", () => acao(() => api(`/tarefas/${id}/cancelar`, {})), "perigo");
  let aberto = true;
  let lidas = 0;
  const d = dialogo([
    h("h2", {}, titulo || `Tarefa ${id}`), h("p", {}, estado), saida,
    h("div", { classe: "rodape-dialogo" }, cancelar, botao("Fechar", () => d.close())),
  ], true);
  // Ao fechar, a página de fundo mostra o resultado da tarefa. Na Configuração, recarregar perderia o que foi digitado.
  d.addEventListener("close", () => { aberto = false; if (rotaAtual() !== "config") rerender(); });
  (async function ciclo() {
    while (aberto) {
      let t;
      try { t = await api(`/tarefas/${id}${consulta({ desde: lidas })}`); }
      catch (e) { saida.append(`\n[!] ${e.message}`); return; }
      if (t.saida.length) { saida.append(t.saida.join("\n") + "\n"); saida.scrollTop = saida.scrollHeight; }
      lidas = t.total_linhas;
      estado.replaceChildren(seloTarefa(t.estado), t.fim ? `  terminou em ${t.fim}` : "");
      if (t.estado !== "executando") {
        cancelar.remove();
        if (!t.saida.length && !lidas) saida.append("(sem saída)");
        carregarListas();
        return;
      }
      await new Promise((ok) => setTimeout(ok, 1200));
    }
  })();
}

// ---------------------------------------------------------- páginas ----

const Paginas = {};

Paginas.painel = async () => {
  const d = await api("/painel");
  const alertas = d.alertas.length
    ? d.alertas.map((a) => h("div", { classe: "alerta " + a.nivel }, a.texto, pequeno("Abrir", () => { location.hash = a.ir; })))
    : [h("div", { classe: "alerta ok" }, "Nenhuma pendência.")];
  if (!d.systemd) alertas.unshift(h("div", { classe: "alerta info" }, "systemd não encontrado neste computador: serviços e timers não podem ser controlados aqui."));
  return [
    cabecalho("Painel", botao("Atualizar tudo agora", () => iniciarTarefa("atualizar"), "primario")),
    h("div", { classe: "cartao" }, h("h2", {}, "O que precisa de atenção"), alertas),
    h("div", { classe: "grade" },
      h("div", { classe: "cartao" }, h("h2", {}, `Medições em ${d.mes}`),
        tabela([
          { titulo: "Medição", valor: (m) => m.relatorio },
          { titulo: "Itens", classe: "num", valor: (m) => m.itens },
          { titulo: "Quedas", classe: "num", valor: (m) => m.quedas },
          { titulo: "Em aberto", classe: "num", valor: (m) => (m.abertas ? selo(m.abertas, "erro") : "0") },
        ], d.medicoes, {
          vazio: "Nenhum item cadastrado. Rode a sincronização em Tarefas.",
          aoClicar: (m) => { (filtros.quedas = filtros.quedas || {}).relatorio = m.relatorio; location.hash = "quedas"; },
        })),
      h("div", { classe: "cartao" }, h("h2", {}, "Banco de dados"),
        h("dl", { classe: "dados" },
          h("dt", {}, "Arquivo"), h("dd", { classe: "mono" }, d.banco.caminho),
          h("dt", {}, "Tamanho"), h("dd", {}, tamanho(d.banco.tamanho)),
          h("dt", {}, "Disco livre"), h("dd", {}, `${tamanho(d.banco.disco_livre)} de ${tamanho(d.banco.disco_total)}`),
          h("dt", {}, "Última queda gravada"), h("dd", {}, d.banco.ultima_alteracao || "-")))),
  ];
};

Paginas.quedas = async () => {
  const atual = (filtros.quedas = filtros.quedas || {});
  atual.relatorio = atual.relatorio || LISTAS.medicoes[0] || "";
  atual.mes = atual.mes || LISTAS.mes_atual;
  const itens = atual.relatorio ? await api("/quedas/itens" + consulta({ relatorio: atual.relatorio, mes: atual.mes })) : [];
  if (atual.item && !itens.some((i) => i.item === atual.item)) atual.item = "";   // trocou de medição ou de mês
  const f = barraFiltros("quedas", [
    { nome: "relatorio", rotulo: "Medição", tipo: "select", opcoes: opcoesMedicao() },
    { nome: "mes", rotulo: "Mês", tipo: "month" },
    { nome: "item", rotulo: "Item", tipo: "select", opcoes: [{ valor: "", rotulo: "Todos os itens" },
      ...itens.map((i) => ({ valor: i.item, rotulo: `${i.item} (${i.quedas})` }))] },
    { nome: "situacao", rotulo: "Situação", tipo: "select", opcoes: [
      { valor: "", rotulo: "Todas" }, { valor: "com-chamado", rotulo: "Com chamado" }, { valor: "abertas", rotulo: "Em aberto" },
      { valor: "contabilizadas", rotulo: "Contando no cálculo" }, { valor: "desconsideradas", rotulo: "Desconsideradas" }] },
  ], rerender);
  const topo = [cabecalho("Quedas", botao("Por lista de chamados", () => dialogoListaChamados())),
    h("p", { classe: "nota" }, "Escolha o item, marque as quedas e clique em Desconsiderar. Elas saem do cálculo, mas continuam no relatório, marcadas com o motivo e registradas em seu nome."),
    f.el];
  if (!f.estado.relatorio) return [...topo, h("div", { classe: "cartao vazio" }, "Nenhuma medição cadastrada. Rode a sincronização em Tarefas.")];
  const linhas = await api("/quedas" + consulta(f.estado));
  const marcadas = new Set();
  const n = h("b", {}, "0");
  const barra = h("div", { classe: "selecao", hidden: true }, n, " selecionada(s)",
    botao("Desconsiderar", () => dialogoDesconsiderar([...marcadas]), "primario"),
    botao("Voltar a contar", () => voltarAContar([...marcadas])));
  const caixas = [];
  const todas = h("input", {
    type: "checkbox", "aria-label": "Marcar todas as quedas da lista",
    onchange: (ev) => {
      caixas.forEach(({ id, caixa }) => { caixa.checked = ev.target.checked; if (ev.target.checked) marcadas.add(id); else marcadas.delete(id); });
      atualizar();
    },
  });
  const atualizar = () => {
    n.textContent = marcadas.size;
    barra.hidden = marcadas.size === 0;
    todas.checked = caixas.length > 0 && marcadas.size === caixas.length;
    todas.indeterminate = marcadas.size > 0 && marcadas.size < caixas.length;
  };
  const caixaDaQueda = (q) => {
    const caixa = h("input", {
      type: "checkbox", "aria-label": `Marcar a queda ${q.ID}`, onclick: (ev) => ev.stopPropagation(),
      onchange: (ev) => { if (ev.target.checked) marcadas.add(q.ID); else marcadas.delete(q.ID); atualizar(); },
    });
    caixas.push({ id: q.ID, caixa });
    return caixa;
  };
  const situacao = (q) => {
    if (q.EXPURGO === "total") return selo("não contabilizada", "aviso");
    if (q.EXPURGO === "parcial") return selo("parcial", "aviso");
    return q.FIM ? selo("contando") : selo("em aberto", "erro");
  };
  return [...topo, h("div", { classe: "cartao" }, barra, tabela([
    { titulo: todas, valor: caixaDaQueda },
    { titulo: "Chamado", valor: (q) => [h("b", {}, q.CHAMADO || "Sem chamado"), h("br"), h("small", {}, q.CHAMADO_TITULO || q.PROBLEMA || "")] },
    { titulo: "Item", valor: (q) => q.ITEM },
    { titulo: "Início", classe: "nw", valor: (q) => q.INICIO },
    { titulo: "Fim", classe: "nw", valor: (q) => q.FIM || "em aberto" },
    { titulo: "Min.", classe: "num", valor: (q) => q.MINUTOS },
    { titulo: "Situação", valor: situacao },
  ], linhas, { vazio: "Nenhuma queda com estes filtros.", aoClicar: (q) => detalheQueda(q.ID) }))];
};

function dialogoListaChamados(texto) {
  abrirForm({
    titulo: "Quedas por lista de chamados", botaoTexto: "Conferir",
    nota: "Cole os números separados por vírgula, espaço ou um por linha. Nada é alterado nesta etapa.",
    campos: [{ nome: "chamados", rotulo: "Chamados", tipo: "area", linhas: 7, obrigatorio: true, valor: texto }],
    enviar: async (v) => {
      const r = await api("/quedas/conferir-chamados", { texto: v.chamados });
      dialogoChamadosConferidos(r, v.chamados);
    },
  });
}

function dialogoChamadosConferidos(r, texto) {
  const pendentes = r.quedas.filter((q) => q.expurgo !== "total");
  const desfazer = r.quedas.filter((q) => q.expurgo);
  const achados = new Set(r.quedas.map((q) => q.chamado)).size;
  abrirForm({
    titulo: `${r.quedas.length} queda(s) em ${achados} chamado(s)`, largo: true,
    botaoTexto: pendentes.length ? `Desconsiderar ${pendentes.length} queda(s)` : "Fechar",
    conteudo: [
      r.nao_encontrados.length ? h("div", { classe: "alerta aviso" },
        `Não encontrados, nada será feito com eles: ${r.nao_encontrados.join(", ")}`) : null,
      tabela([
        { titulo: "Chamado", classe: "nw", valor: (q) => q.chamado },
        { titulo: "Medição", valor: (q) => q.relatorio },
        { titulo: "Item", valor: (q) => q.item },
        { titulo: "Início", classe: "nw", valor: (q) => q.inicio },
        { titulo: "Fim", classe: "nw", valor: (q) => q.fim || "em aberto" },
        { titulo: "Min.", classe: "num", valor: (q) => q.minutos },
        { titulo: "Situação", valor: (q) => (q.expurgo === "total" ? selo("não contabilizada", "aviso")
          : q.expurgo === "parcial" ? selo("parcial", "aviso") : selo("contando")) },
      ], r.quedas, { vazio: "Nenhuma queda encontrada para estes chamados." }),
    ],
    campos: pendentes.length ? [
      { nome: "tipo", rotulo: "Tipo", tipo: "select", opcoes: opcoesTipo() },
      { nome: "motivo", rotulo: "Motivo (aparece no relatório)", tipo: "area", obrigatorio: true },
    ] : [],
    extras: (fechar) => [
      botao("Alterar a lista", () => { fechar(); dialogoListaChamados(texto); }),
      desfazer.length ? botao(`Voltar a contar ${desfazer.length} queda(s)`, () => { fechar(); voltarAContar(desfazer.map((q) => q.id)); }) : null,
    ],
    enviar: async (v) => (pendentes.length
      ? api("/quedas/desconsiderar", { incidentes: pendentes.map((q) => q.id), tipo: v.tipo, motivo: v.motivo })
      : undefined),
  });
}

function dialogoDesconsiderar(ids) {
  const um = ids.length === 1;
  abrirForm({
    titulo: um ? `Desconsiderar a queda #${ids[0]}` : `Desconsiderar ${ids.length} quedas`,
    botaoTexto: "Desconsiderar",
    campos: [
      { nome: "tipo", rotulo: "Tipo", tipo: "select", opcoes: opcoesTipo() },
      { nome: "motivo", rotulo: "Motivo (aparece no relatório)", tipo: "area", obrigatorio: true },
      ...(um ? [[
        { nome: "inicio", rotulo: "Só um trecho: início (Brasília)", tipo: "datetime-local", ajuda: "Vazio = a queda inteira" },
        { nome: "fim", rotulo: "Só um trecho: fim (Brasília)", tipo: "datetime-local" },
      ]] : []),
    ],
    enviar: (v) => api("/quedas/desconsiderar", { incidentes: ids, tipo: v.tipo, motivo: v.motivo, inicio: v.inicio || null, fim: v.fim || null }),
  });
}

function voltarAContar(ids) {
  confirmar({
    titulo: "Voltar a contar",
    texto: `Os expurgos de ${ids.length} queda(s) serão desfeitos e elas voltam a contar na disponibilidade.`,
    botaoTexto: "Voltar a contar", enviar: () => api("/quedas/contabilizar", { incidentes: ids }),
  });
}

async function detalheQueda(id) {
  let d;
  try { d = await api(`/quedas/${id}`); } catch (e) { return toast(e.message, "erro"); }
  const dlg = dialogo([
    h("h2", {}, `Queda #${d.id} - ${d.item}`),
    h("dl", { classe: "dados" },
      h("dt", {}, "Medição"), h("dd", {}, d.relatorio),
      h("dt", {}, "Grupo"), h("dd", {}, d.grupo || "-"),
      h("dt", {}, "Problema"), h("dd", {}, d.problema || "-"),
      h("dt", {}, "Período"), h("dd", {}, `${d.inicio} até ${d.fim || "em aberto"}`),
      h("dt", {}, "Chamado"), h("dd", {}, d.chamado_numero || "-"),
      d.chamado_externo ? [h("dt", {}, "Protocolo externo"), h("dd", {}, d.chamado_externo)] : null,
      d.causa ? [h("dt", {}, "Causa"), h("dd", {}, d.causa)] : null),
    d.expurgos.length ? [h("h2", { style: "margin-top:14px" }, "Expurgos"),
      d.expurgos.map((e) => h("div", { classe: "bloco" }, seloExpurgo(e.status), ` #${e.id} ${e.tipo_nome}: ${e.motivo} `,
        h("small", {}, `(${e.inicio} a ${e.fim}, por ${e.proposto_por})`)))] : null,
    d.chamado ? [h("h2", { style: "margin-top:14px" }, `Chamado ${d.chamado.numero}: ${d.chamado.titulo || ""}`),
      h("p", { classe: "nota" }, `${d.chamado.status || "-"} · lido do TOPdesk em ${d.chamado.lido_em}`),
      d.chamado.acoes.length ? d.chamado.acoes.map((a) => h("div", { classe: "bloco" },
        h("small", {}, `${a.em || "-"} · ${a.autor || "-"}${a.analista ? "" : " (solicitante/sistema)"}`),
        h("div", { classe: "acao-texto" }, a.texto))) : h("p", { classe: "nota" }, "Sem ações registradas.")]
      : (d.chamado_numero ? h("p", { classe: "nota", style: "margin-top:14px" }, "Chamado ainda não consultado no TOPdesk.") : null),
    d.comentarios.length ? [h("h2", { style: "margin-top:14px" }, "Comentários no Zabbix"),
      d.comentarios.map((c) => h("div", { classe: "bloco" }, h("small", {}, `${c.em} · ${c.usuario || "-"}`), h("div", { classe: "acao-texto" }, c.mensagem)))] : null,
    h("div", { classe: "rodape-dialogo" },
      d.chamado_numero ? botao("Reler chamado", () => { dlg.close(); iniciarTarefa("importar-chamados", { chamado: d.chamado_numero }); }) : null,
      botao("Desconsiderar", () => { dlg.close(); dialogoDesconsiderar([d.id]); }),
      d.expurgos.some((e) => e.status === "aprovado") ? botao("Voltar a contar", () => { dlg.close(); voltarAContar([d.id]); }) : null,
      botao("Fechar", () => dlg.close(), "primario")),
  ], true);
}

function seloExpurgo(s) {
  return selo(s, { aprovado: "ok", proposto: "aviso", rejeitado: "" }[s]);
}

Paginas.expurgos = async () => {
  const f = barraFiltros("expurgos", [
    { nome: "relatorio", rotulo: "Medição", tipo: "select", opcoes: opcoesMedicao(true) },
    { nome: "mes", rotulo: "Mês", tipo: "month", valor: LISTAS.mes_atual },
    { nome: "status", rotulo: "Status", tipo: "select", opcoes: [
      { valor: "", rotulo: "Todos" }, { valor: "proposto", rotulo: "Propostos" },
      { valor: "aprovado", rotulo: "Aprovados" }, { valor: "rejeitado", rotulo: "Rejeitados / desfeitos" }] },
  ], rerender);
  const linhas = await api("/expurgos" + consulta(f.estado));
  const decidir = (e, op, titulo, texto) => confirmar({
    titulo: `${titulo} o expurgo #${e.id}`, texto, botaoTexto: titulo, perigo: op !== "aprovar",
    enviar: () => api(`/expurgos/${e.id}/${op}`, {}),
  });
  return [
    cabecalho("Expurgos", botao("Novo expurgo de janela", dialogoExpurgoNovo, "primario")),
    h("p", { classe: "nota" }, "Para uma queda específica, use a aba Quedas. Aqui você cria uma janela (por exemplo, manutenção programada), que só vale no cálculo depois de aprovada."),
    f.el,
    h("div", { classe: "cartao" }, tabela([
      { titulo: "Id", classe: "num", valor: (e) => e.id },
      { titulo: "Status", valor: (e) => seloExpurgo(e.status) },
      { titulo: "Medição / item", valor: (e) => [e.relatorio, h("br"), h("small", {}, e.item)] },
      { titulo: "Período", classe: "nw", valor: (e) => [e.inicio, h("br"), e.fim] },
      { titulo: "Tipo e motivo", valor: (e) => [h("b", {}, e.tipo_nome), h("br"), e.motivo] },
      { titulo: "Por", valor: (e) => [e.proposto_por, e.decidido_por ? [h("br"), h("small", {}, `decidido: ${e.decidido_por}`)] : null] },
      { titulo: "", valor: (e) => h("div", { classe: "acoes" },
        e.status === "proposto" ? [
          pequeno("Aprovar", () => decidir(e, "aprovar", "Aprovar", "O período passa a ser descontado da indisponibilidade.")),
          pequeno("Rejeitar", () => decidir(e, "rejeitar", "Rejeitar", "O expurgo não terá efeito no cálculo."), "perigo")] : null,
        e.status === "aprovado" ? pequeno("Desfazer", () => decidir(e, "revogar", "Desfazer", "O período volta a contar como indisponibilidade."), "perigo") : null) },
    ], linhas, { vazio: "Nenhum expurgo com estes filtros." })),
  ];
};

async function dialogoExpurgoNovo() {
  if (!LISTAS.medicoes.length) return toast("Nenhuma medição cadastrada.", "erro");
  const d = abrirForm({
    titulo: "Novo expurgo de janela", botaoTexto: "Propor expurgo",
    nota: "Datas e horas no horário de Brasília. Depois de criado, ele aparece como proposto, para aprovação.",
    campos: [
      [{ nome: "relatorio", rotulo: "Medição", tipo: "select", opcoes: opcoesMedicao() },
        { nome: "objeto", rotulo: "Item", tipo: "select", opcoes: [{ valor: "", rotulo: "Todos os itens da medição" }] }],
      [{ nome: "inicio", rotulo: "Início", tipo: "datetime-local", obrigatorio: true },
        { nome: "fim", rotulo: "Fim", tipo: "datetime-local", obrigatorio: true }],
      [{ nome: "tipo", rotulo: "Tipo", tipo: "select", opcoes: opcoesTipo() },
        { nome: "chamado", rotulo: "Chamado (opcional)" }],
      { nome: "motivo", rotulo: "Motivo (aparece no relatório)", tipo: "area", obrigatorio: true },
    ],
    enviar: (v) => api("/expurgos", { ...v, objeto: v.objeto ? Number(v.objeto) : null }),
  });
  const sel = d.querySelector("[name=relatorio]");
  const itens = d.querySelector("[name=objeto]");
  const carregar = async () => {
    const lista = await api("/itens" + consulta({ relatorio: sel.value })).catch(() => []);
    itens.replaceChildren(h("option", { value: "" }, "Todos os itens da medição"), ...lista.map((i) => h("option", { value: i.id }, i.item)));
  };
  sel.addEventListener("change", carregar);
  carregar();
}

Paginas.lacunas = async () => {
  const linhas = await api("/lacunas");
  return [
    cabecalho("Lacunas de monitoramento", botao("Registrar lacuna", () => abrirForm({
      titulo: "Registrar lacuna", botaoTexto: "Registrar",
      nota: "O período sai do cálculo: não conta como disponível nem como indisponível. Horário de Brasília.",
      campos: [
        { nome: "relatorio", rotulo: "Medição", tipo: "select", opcoes: opcoesMedicao(true, "Todas as medições") },
        [{ nome: "inicio", rotulo: "Início", tipo: "datetime-local", obrigatorio: true },
          { nome: "fim", rotulo: "Fim", tipo: "datetime-local", obrigatorio: true }],
        { nome: "motivo", rotulo: "Motivo", tipo: "area", obrigatorio: true },
      ],
      enviar: (v) => api("/lacunas", v),
    }), "primario")),
    h("p", { classe: "nota" }, "Use quando o próprio monitoramento ficou fora do ar (Zabbix, proxy)."),
    h("div", { classe: "cartao" }, tabela([
      { titulo: "Medição", valor: (l) => l.relatorio || "Todas" },
      { titulo: "Início", classe: "nw", valor: (l) => l.inicio },
      { titulo: "Fim", classe: "nw", valor: (l) => l.fim },
      { titulo: "Min.", classe: "num", valor: (l) => l.minutos },
      { titulo: "Motivo", valor: (l) => l.motivo },
      { titulo: "Por", valor: (l) => l.criado_por },
      { titulo: "", valor: (l) => pequeno("Remover", () => confirmar({
        titulo: "Remover lacuna", texto: `O período de ${l.inicio} a ${l.fim} volta a entrar no cálculo.`,
        botaoTexto: "Remover", enviar: () => api(`/lacunas/${l.id}/remover`, {}),
      }), "perigo") },
    ], linhas, { vazio: "Nenhuma lacuna registrada." })),
  ];
};

function dialogoRemoverItem() {
  if (!LISTAS.medicoes.length) return toast("Nenhuma medição cadastrada.", "erro");
  const d = abrirForm({
    titulo: "Remover um item", botaoTexto: "Conferir",
    nota: "Primeiro tire a tag da trigger no Zabbix. Nada é alterado nesta etapa.",
    campos: [
      { nome: "relatorio", rotulo: "Medição", tipo: "select", opcoes: opcoesMedicao() },
      { nome: "objeto", rotulo: "Item", tipo: "select", opcoes: [], obrigatorio: true },
    ],
    enviar: (v) => conferirRemocao(Number(v.objeto)),
  });
  const sel = d.querySelector("[name=relatorio]");
  const itens = d.querySelector("[name=objeto]");
  const carregar = async () => {
    const lista = await api("/itens" + consulta({ relatorio: sel.value, estado: "todos" })).catch(() => []);
    lista.sort((a, b) => (!a.ate - !b.ate) || a.item.localeCompare(b.item));    // encerrados primeiro
    itens.replaceChildren(...lista.map((i) => h("option", { value: i.id }, `${i.item}${i.ate ? " (encerrado)" : ""}`)));
  };
  sel.addEventListener("change", carregar);
  carregar();
}

async function conferirRemocao(id) {
  const d = await api(`/itens/${id}/remocao`);
  if (d.ativo) {
    abrirForm({
      titulo: `${d.item} ainda está ativo`, campos: [], botaoTexto: "Sincronizar agora", perigo: false,
      nota: "Com a tag ainda no Zabbix, ele voltaria na próxima sincronização, em até 10 minutos. Tire a tag da trigger, sincronize e tente de novo. Se a tag já saiu, basta sincronizar.",
      enviar: () => { iniciarTarefa("sync-objetos"); return Promise.resolve(); },
    });
    return;
  }
  abrirForm({
    titulo: `Remover ${d.item}`, perigo: true, botaoTexto: "Remover definitivamente",
    nota: "O item some de todos os períodos, inclusive dos anteriores, e as médias da medição mudam. O painel faz uma cópia do banco antes.",
    conteudo: h("dl", { classe: "dados", style: "margin-bottom:12px" },
      h("dt", {}, "Medição"), h("dd", {}, d.relatorio),
      h("dt", {}, "Quedas"), h("dd", {}, d.incidentes),
      h("dt", {}, "Comentários"), h("dd", {}, d.comentarios),
      h("dt", {}, "Expurgos"), h("dd", {}, d.expurgos),
      h("dt", {}, "Chamados ligados"), h("dd", {}, d.chamados)),
    campos: d.meses_fechados.length ? [{
      nome: "incluir_fechados", tipo: "marcar",
      rotulo: `Apagar também os valores congelados de ${d.meses_fechados.join(", ")}. O PDF oficial só muda se você gerar uma nova versão.`,
    }] : [],
    enviar: async (v) => {
      const r = await api(`/itens/${id}/remover`, { incluir_fechados: v.incluir_fechados === true });
      await carregarListas();
      return r;
    },
  });
}

Paginas.itens = async () => {
  const f = barraFiltros("itens", [
    { nome: "relatorio", rotulo: "Medição", tipo: "select", opcoes: opcoesMedicao(true) },
    { nome: "estado", rotulo: "Estado", tipo: "select", opcoes: [
      { valor: "ativos", rotulo: "Ativos" }, { valor: "encerrados", rotulo: "Encerrados" }, { valor: "todos", rotulo: "Todos" }], valor: "ativos" },
    { nome: "mes", rotulo: "Mês", tipo: "month", valor: LISTAS.mes_atual },
  ], rerender);
  const linhas = await api("/itens" + consulta(f.estado));
  const pct = (v) => (v === null || v === undefined ? "-" : v.toFixed(2).replace(".", ",") + "%");
  return [
    cabecalho("Itens medidos", botao("Remover um item", dialogoRemoverItem), botao("Sincronizar com o Zabbix", () => iniciarTarefa("sync-objetos"), "primario")),
    h("p", { classe: "nota" }, "Os itens vêm das tags das triggers no Zabbix: para incluir, tirar ou renomear, altere lá. A sincronização roda a cada 10 minutos. Sem a tag, o item é encerrado: some dos meses seguintes, mas continua nos meses em que existiu, até você removê-lo."),
    f.el,
    h("div", { classe: "cartao" }, h("p", { classe: "nota" }, `${linhas.length} item(ns)`), tabela([
      { titulo: "Medição", valor: (i) => i.relatorio },
      { titulo: "Item", valor: (i) => [i.item, i.macro_pendente ? [" ", selo("macro sem valor", "erro")] : null] },
      { titulo: "Grupo", valor: (i) => i.grupo || "-" },
      { titulo: "Descrição", valor: (i) => i.descricao || "-" },
      { titulo: "Medido desde", classe: "nw", valor: (i) => i.desde },
      { titulo: "Encerrado em", classe: "nw", valor: (i) => i.ate || "-" },
      { titulo: "Quedas", classe: "num", valor: (i) => i.quedas },
      { titulo: "Min. fora", classe: "num", valor: (i) => i.minutos_fora },
      { titulo: "Disponib.", classe: "num", valor: (i) => pct(i.disponibilidade) },
      { titulo: "", valor: (i) => (i.ate ? pequeno("Remover", () => acao(() => conferirRemocao(i.id)), "perigo") : null) },
    ], linhas, { vazio: "Nenhum item com estes filtros." })),
  ];
};

Paginas.fechamento = async () => {
  const f = barraFiltros("fechamento", [{ nome: "mes", rotulo: "Mês", tipo: "month", valor: LISTAS.mes_anterior }], rerender);
  const d = await api("/fechamentos" + consulta(f.estado));
  const fechar = (r, nova) => confirmar({
    titulo: nova ? `Nova versão de ${r.id}` : `Fechar ${r.id}`,
    texto: nova
      ? `Gera a v${r.versoes[0].versao + 1} de ${d.mes} com os dados de agora. As versões anteriores continuam guardadas.`
      : `Congela os valores de ${d.mes} e guarda o PDF oficial. Depois disso, mudanças só valem com uma nova versão.`,
    botaoTexto: nova ? "Gerar nova versão" : "Fechar mês", perigo: false,
    enviar: () => { iniciarTarefa("fechar", { relatorio: r.id, mes: d.mes, nova_versao: nova }); return Promise.resolve(); },
  });
  return [
    cabecalho("Fechamento mensal", botao(`Fechar todos de ${LISTAS.mes_anterior}`, () => confirmar({
      titulo: `Fechar todos os relatórios de ${LISTAS.mes_anterior}`,
      texto: "Fecha os que ainda não foram fechados. É o mesmo que o timer faz todo dia 1º às 06:00.",
      botaoTexto: "Fechar todos", perigo: false,
      enviar: () => { iniciarTarefa("fechar-mes-anterior"); return Promise.resolve(); },
    }), "primario")),
    f.el,
    !d.terminou ? h("div", { classe: "alerta info" }, `${d.mes} ainda não terminou: só pode ser fechado depois do último dia.`) : null,
    h("div", { classe: "cartao" }, tabela([
      { titulo: "Relatório", valor: (r) => [h("b", {}, r.id), h("br"), h("small", {}, r.titulo)] },
      { titulo: "Situação", valor: (r) => (r.versoes.length ? selo(`fechado v${r.versoes[0].versao}`, "ok") : selo("sem fechamento", d.terminou ? "aviso" : "")) },
      { titulo: "Fechado em", classe: "nw", valor: (r) => (r.versoes[0] ? `${r.versoes[0].gerado_em} por ${r.versoes[0].gerado_por}` : "-") },
      { titulo: "PDFs", valor: (r) => r.versoes.filter((v) => v.pdf).map((v) => h("div", {}, h("a", { href: "/api/pdf" + consulta({ caminho: v.pdf }) }, `v${v.versao}`))) },
      { titulo: "", valor: (r) => (d.terminou && r.titulo !== "(definição removida)"
        ? pequeno(r.versoes.length ? "Nova versão" : "Fechar", () => fechar(r, r.versoes.length > 0)) : null) },
    ], d.relatorios)),
  ];
};

const dataBr = (iso) => `${iso.slice(8, 10)}/${iso.slice(5, 7)}/${iso.slice(0, 4)}`;

Paginas.relatorios = async () => {
  const [d, pdfs] = await Promise.all([api("/periodos"), api("/pdfs")]);
  const estado = (filtros.relatorios = filtros.relatorios || { periodo: "30d", relatorio: "", de: "", ate: "" });
  const de = h("input", { name: "de", type: "date", value: estado.de, max: d.hoje });
  const ate = h("input", { name: "ate", type: "date", value: estado.ate, max: d.hoje });
  const aviso = h("div", { classe: "alerta aviso", hidden: true });
  const livres = h("div", { classe: "filtros", hidden: estado.periodo !== "outro" },
    h("label", { classe: "campo" }, "De", de), h("label", { classe: "campo" }, "Até", ate));
  const antigo = () => estado.periodo === "outro" && de.value && de.value < d.dados_desde;
  const atualizar = () => {
    livres.hidden = estado.periodo !== "outro";
    de.required = ate.required = estado.periodo === "outro";
    aviso.hidden = !antigo();
    aviso.textContent = `As quedas anteriores a ${dataBr(d.dados_desde)} já foram apagadas pela limpeza automática. Nesse trecho, a disponibilidade sairia maior do que foi de verdade.`;
  };
  de.addEventListener("input", atualizar);
  const opcao = (id, rotulo, datas) => h("label", { classe: "chip" },
    h("input", { type: "radio", name: "periodo", value: id, checked: estado.periodo === id,
      onchange: () => { estado.periodo = id; atualizar(); } }),
    h("span", {}, h("b", {}, rotulo), datas ? h("small", {}, datas) : null));
  const gerar = (parametros) => iniciarTarefa("gerar", parametros);
  const form = h("form", {
    classe: "cartao formulario", onsubmit: (ev) => {
      ev.preventDefault();
      estado.relatorio = form.elements.relatorio.value;
      estado.de = de.value;
      estado.ate = ate.value;
      const p = d.periodos.find((x) => x.id === estado.periodo);
      const tempo = p ? (p.mes ? { mes: p.mes } : { de: p.de, ate: p.ate }) : { de: de.value, ate: ate.value };
      const parametros = { relatorio: estado.relatorio, ...tempo };
      if (!antigo()) return gerar(parametros);
      confirmar({
        titulo: "Período com dados apagados", texto: aviso.textContent, botaoTexto: "Gerar mesmo assim",
        enviar: () => { gerar(parametros); return Promise.resolve(); },
      });
    },
  },
  h("label", { classe: "campo" }, "Relatório", campoEntrada({ nome: "relatorio", tipo: "select", opcoes: opcoesPdf(true), valor: estado.relatorio })),
  h("fieldset", { classe: "chips" }, h("legend", {}, "Período"),
    d.periodos.map((p) => opcao(p.id, p.rotulo, `${dataBr(p.de)} a ${dataBr(p.ate)}`)),
    opcao("outro", "Outro período", "Escolha as datas")),
  livres, aviso,
  h("div", {}, h("button", { type: "submit", classe: "primario" }, "Gerar relatório")));
  atualizar();
  const avulsos = pdfs.filter((p) => p.tipo === "avulso").slice(0, 10);
  return [
    cabecalho("Gerar relatório"),
    h("p", { classe: "nota" }, "Gera o PDF na hora, com os dados de agora. Não muda o fechamento: o PDF oficial do mês continua em Fechamento."),
    form,
    h("div", { classe: "cartao" }, h("h2", {}, "Gerados recentemente"), tabela([
      { titulo: "Arquivo", valor: (p) => h("a", { href: "/api/pdf" + consulta({ caminho: p.caminho }) }, p.nome) },
      { titulo: "Gerado em", classe: "nw", valor: (p) => p.gerado_em },
      { titulo: "Tamanho", classe: "num", valor: (p) => tamanho(p.tamanho) },
    ], avulsos, { vazio: "Nenhum relatório gerado ainda." })),
  ];
};

Paginas.pdfs = async () => {
  const f = barraFiltros("pdfs", [
    { nome: "tipo", rotulo: "Tipo", tipo: "select", opcoes: [
      { valor: "", rotulo: "Todos" }, { valor: "oficial", rotulo: "Oficiais" }, { valor: "parcial", rotulo: "Parciais" }, { valor: "avulso", rotulo: "Avulsos" }] },
  ], rerender);
  const todos = await api("/pdfs");
  const linhas = f.estado.tipo ? todos.filter((p) => p.tipo === f.estado.tipo) : todos;
  return [
    cabecalho("Arquivos PDF", botao("Gerar relatório", () => { location.hash = "relatorios"; }, "primario")),
    f.el,
    h("div", { classe: "cartao" }, tabela([
      { titulo: "Arquivo", valor: (p) => [h("a", { href: "/api/pdf" + consulta({ caminho: p.caminho }) }, p.nome), h("br"), h("small", {}, p.pasta)] },
      { titulo: "Tipo", valor: (p) => selo(p.tipo, { oficial: "ok", parcial: "info" }[p.tipo]) },
      { titulo: "Gerado em", classe: "nw", valor: (p) => p.gerado_em },
      { titulo: "Tamanho", classe: "num", valor: (p) => tamanho(p.tamanho) },
      { titulo: "", valor: (p) => (p.tipo === "avulso" ? pequeno("Remover", () => confirmar({
        titulo: "Remover PDF avulso", texto: p.nome, botaoTexto: "Remover", enviar: () => api("/pdfs/remover", { caminho: p.caminho }),
      }), "perigo") : null) },
    ], linhas, { vazio: "Nenhum PDF encontrado." })),
  ];
};

Paginas.definicoes = async () => {
  const d = await api("/definicoes");
  const editar = (id, def) => {
    const novo = !id;
    const secoes = JSON.stringify(def?.secoes ?? [{ tipo: "indicadores", titulo: "Resumo do mês", indicadores: [{ rotulo: "Disponibilidade geral", chave: "GERAL", relatorio: d.medicoes[0] || "id-da-medicao" }] }], null, 2);
    abrirForm({
      titulo: novo ? "Novo relatório" : `Editar ${id}`, largo: true, botaoTexto: "Salvar",
      nota: `Medições no banco: ${d.medicoes.join(", ") || "nenhuma"}. Uma cópia da versão anterior fica em Backups.`,
      campos: [
        ...(novo ? [{ nome: "id", rotulo: "Id", obrigatorio: true, ajuda: "Minúsculas, números e hífen. Não pode ser mudado depois." }] : []),
        [{ nome: "titulo", rotulo: "Título", valor: def?.titulo, obrigatorio: true }, { nome: "subtitulo", rotulo: "Subtítulo", valor: def?.subtitulo }],
        [{ nome: "cliente", rotulo: "Cliente", valor: def?.cliente ?? "ENGIE" }, { nome: "meta", rotulo: "Meta (%)", tipo: "number", step: "0.01", min: 0, max: 100, valor: def?.meta ?? 99.9, obrigatorio: true }],
        { nome: "arquivo", rotulo: "Nome do arquivo PDF", valor: def?.arquivo, obrigatorio: true, ajuda: "Pode usar {mes_nome}, {ano} e {mes}." },
        { nome: "secoes", rotulo: "Seções (JSON)", tipo: "area", classe: "json", valor: secoes, obrigatorio: true,
          ajuda: "Tipos: indicadores, tabela, grupos, evidencias, chamados. A ordem aqui é a ordem no PDF." },
      ],
      enviar: async (v) => {
        let lista;
        try { lista = JSON.parse(v.secoes); } catch (e) { throw new Error("Seções: JSON inválido (" + e.message + ")"); }
        const definicao = { ...(def || {}), cliente: v.cliente, titulo: v.titulo, subtitulo: v.subtitulo, arquivo: v.arquivo, meta: Number(v.meta), secoes: lista };
        const alvo = novo ? v.id.trim() : id;
        if (!/^[a-z0-9][a-z0-9_-]{0,60}$/.test(alvo)) throw new Error("Id inválido: use minúsculas, números e hífen.");
        const r = await api("/definicoes/" + alvo, { definicao, novo });
        carregarListas();
        return r;
      },
    });
  };
  return [
    cabecalho("Definições dos relatórios", botao("Novo relatório", () => editar(null, null), "primario")),
    h("p", { classe: "nota" }, "Definem o que entra em cada PDF. Relatórios aqui entram sozinhos no parcial diário e no fechamento. A definição é conferida antes de gravar."),
    h("div", { classe: "cartao" }, tabela([
      { titulo: "Id", valor: ([id]) => h("code", {}, id) },
      { titulo: "Título", valor: ([, v]) => v.titulo },
      { titulo: "Seções", classe: "num", valor: ([, v]) => (v.secoes || []).length },
      { titulo: "Meta", classe: "num", valor: ([, v]) => `${String(v.meta ?? 99.9).replace(".", ",")}%` },
      { titulo: "", valor: ([id, v]) => h("div", { classe: "acoes" },
        pequeno("Editar", () => editar(id, v)),
        pequeno("Remover", () => confirmar({
          titulo: `Remover ${id}`, texto: "O relatório sai do parcial diário e dos próximos fechamentos. PDFs já gerados continuam guardados.",
          botaoTexto: "Remover", enviar: async () => { const r = await api(`/definicoes/${id}/remover`, {}); carregarListas(); return r; },
        }), "perigo")) },
    ], Object.entries(d.definicoes))),
  ];
};

const CATALOGO = [
  { tipo: "atualizar", titulo: "Atualizar tudo agora", texto: "O mesmo ciclo do timer de 10 minutos: itens, quedas, chamados e limpeza." },
  { tipo: "carga-inicial", titulo: "Carga inicial", texto: "Cadastra os itens e importa o histórico desde a data. Pode repetir sem duplicar.",
    campos: [{ nome: "desde", rotulo: "Primeiro dia a medir", tipo: "date", obrigatorio: true, valor: () => `${LISTAS.mes_atual}-01` }] },
  { tipo: "sync-objetos", titulo: "Sincronizar itens", texto: "Lê as triggers com a tag relatorio no Zabbix.",
    campos: [{ nome: "desde", rotulo: "Início da medição dos itens novos", tipo: "date" }] },
  { tipo: "importar-eventos", titulo: "Importar quedas", texto: "Busca os problemas no Zabbix. Pode repetir sem duplicar.",
    campos: [{ nome: "de", rotulo: "De", tipo: "date", obrigatorio: true }, { nome: "ate", rotulo: "Até", tipo: "date", obrigatorio: true }] },
  { tipo: "importar-chamados", titulo: "Importar chamados", texto: "Busca chamados e ações no TOPdesk.",
    campos: [{ nome: "dias", rotulo: "Últimos dias", tipo: "number", valor: 45, min: 1, max: 365 }, { nome: "chamado", rotulo: "Ou um número", placeholder: "I2611-12345" }] },
  { tipo: "parciais", titulo: "Gerar PDFs parciais", texto: "Refaz o parcial de todos os relatórios.",
    campos: [{ nome: "mes", rotulo: "Mês", tipo: "month", valor: () => LISTAS.mes_atual }] },
  { tipo: "limpar", titulo: "Limpar dados antigos", texto: "Apaga de vez quedas, expurgos, lacunas e chamados antigos. Faça uma cópia antes.", perigo: true,
    aviso: (v) => `Os dados anteriores aos últimos ${v.meses} meses serão apagados definitivamente. O timer usa 3 meses.`,
    campos: [{ nome: "meses", rotulo: "Meses a manter", tipo: "number", valor: 3, min: 1, max: 120, obrigatorio: true }] },
  { titulo: "Remover um item", texto: "Apaga de vez um item e suas quedas, de todos os períodos. Primeiro tire a tag da trigger no Zabbix.", perigo: true, abrir: dialogoRemoverItem },
  { tipo: "criar-triggers", titulo: "Simular triggers TID", texto: "Mostra as triggers que faltam nos hosts ENGIE APP - TID, sem criar nada." },
  { tipo: "criar-triggers", titulo: "Criar triggers TID", texto: "Cria no Zabbix as triggers que faltam. Rode a simulação antes.", perigo: true, parametros: { aplicar: true },
    aviso: () => "As triggers serão criadas no Zabbix de verdade." },
  { tipo: "testar-zabbix", titulo: "Testar Zabbix", texto: "Confere a URL e o token." },
  { tipo: "testar-topdesk", titulo: "Testar TOPdesk", texto: "Confere a URL, o usuário e a senha de aplicação." },
  { tipo: "testes", titulo: "Testes do cálculo", texto: "Roda os testes do motor. Precisa de python3-pytest." },
];

Paginas.tarefas = async () => {
  const historico = await api("/tarefas");
  const cartao = (t) => {
    const campos = (t.campos || []).map((c) => ({ ...c, valor: typeof c.valor === "function" ? c.valor() : c.valor }));
    const form = h("form", {
      classe: "tarefa", onsubmit: (ev) => {
        ev.preventDefault();
        if (t.abrir) return t.abrir();
        const v = { ...lerForm(form), ...(t.parametros || {}) };
        if (t.perigo) {
          confirmar({ titulo: t.titulo, texto: t.aviso(v), botaoTexto: "Executar",
            enviar: () => { iniciarTarefa(t.tipo, v); return Promise.resolve(); } });
        } else iniciarTarefa(t.tipo, v);
      },
    }, h("b", {}, t.titulo), h("p", {}, t.texto),
    campos.length ? h("div", { classe: "linha" }, campos.map((c) => h("label", { classe: "campo" }, c.rotulo, campoEntrada(c)))) : null,
    h("div", {}, h("button", { type: "submit", classe: t.perigo ? "perigo" : null }, "Executar")));
    return form;
  };
  return [
    cabecalho("Tarefas"),
    h("div", { classe: "grade-tarefas" }, CATALOGO.map(cartao)),
    h("div", { classe: "cartao", style: "margin-top:16px" }, h("h2", {}, "Execuções desde que o painel foi iniciado"), tabela([
      { titulo: "Tarefa", valor: (t) => t.titulo },
      { titulo: "Situação", valor: (t) => seloTarefa(t.estado) },
      { titulo: "Por", valor: (t) => t.por },
      { titulo: "Início", classe: "nw", valor: (t) => t.inicio },
      { titulo: "Fim", classe: "nw", valor: (t) => t.fim || "-" },
    ], historico, { vazio: "Nenhuma execução ainda.", aoClicar: (t) => abrirTarefa(t.id, t.titulo) })),
  ];
};

const ROTULO_ACAO = { start: "Iniciar", restart: "Reiniciar", stop: "Parar", enable: "Habilitar", disable: "Desabilitar" };

// Só oferece o que faz sentido no estado atual da unidade.
function acoesDaUnidade(u) {
  if (u.carregada !== "loaded") return [];
  const ativa = u.ativa === "active" || u.ativa === "activating";
  const permitidas = { start: u.tipo === "avulso" ? u.ativa !== "activating" : !ativa, restart: ativa, stop: ativa,
    enable: u.habilitada !== "enabled", disable: u.habilitada === "enabled" };
  return Object.keys(ROTULO_ACAO).filter((a) => u.acoes.includes(a) && permitidas[a]);
}

Paginas.servicos = async () => {
  const d = await api("/servicos");
  if (!d.disponivel) return [cabecalho("Serviços e logs"), h("div", { classe: "alerta info" }, "systemd não encontrado neste computador. No servidor, esta tela controla a API e os timers.")];
  const estado = (u) => {
    if (u.carregada !== "loaded") return selo("não instalada", "erro");
    if (u.ativa === "failed") return selo("falhou", "erro");
    if (u.tipo === "avulso") return u.ativa === "activating" ? selo("executando", "info") : selo(u.resultado === "success" ? "última OK" : u.resultado, u.resultado === "success" ? "ok" : "erro");
    return u.ativa === "active" ? selo(u.tipo === "timer" ? "agendado" : "no ar", "ok") : selo("parado", "erro");
  };
  const executarAcao = (u, a) => confirmar({
    titulo: `${ROTULO_ACAO[a]}: ${u.titulo}`, texto: u.nome, botaoTexto: ROTULO_ACAO[a], perigo: a === "stop" || a === "disable",
    enviar: async () => { const r = await api(`/servicos/${u.nome}/${a}`, {}); setTimeout(rerender, 1200); return r; },
  });
  const verLogs = async (u) => {
    const saida = h("pre", { classe: "saida" }, "Carregando...");
    const dlg = dialogo([h("h2", {}, u.titulo), h("p", { classe: "nota mono" }, u.nome), saida,
      h("div", { classe: "rodape-dialogo" }, botao("Fechar", () => dlg.close(), "primario"))], true);
    try {
      const r = await api("/logs" + consulta({ unidade: u.nome, linhas: 300 }));
      saida.textContent = r.linhas.join("\n") || "(sem registros)";
      saida.scrollTop = saida.scrollHeight;
    } catch (e) { saida.textContent = e.message; }
  };
  return [
    cabecalho("Serviços e logs", botao("Recarregar", rerender)),
    h("div", { classe: "cartao" }, tabela([
      { titulo: "Unidade", valor: (u) => [h("b", {}, u.titulo), h("br"), h("small", { classe: "mono" }, u.nome)] },
      { titulo: "Estado", valor: estado },
      { titulo: "Inicia no boot", valor: (u) => u.habilitada || "-" },
      { titulo: "Última / próxima", valor: (u) => [u.ultima || u.terminou || "-", u.proxima ? [h("br"), h("small", {}, `próxima: ${u.proxima}`)] : null] },
      { titulo: "", valor: (u) => h("div", { classe: "acoes" },
        pequeno("Logs", () => verLogs(u)),
        acoesDaUnidade(u).map((a) => pequeno(u.tipo === "avulso" ? "Executar agora" : ROTULO_ACAO[a], () => executarAcao(u, a), a === "stop" || a === "disable" ? "perigo" : ""))) },
    ], d.unidades)),
  ];
};

Paginas.config = async () => {
  const d = await api("/config");
  const iniciais = Object.fromEntries(d.campos.map((c) => [c.chave, c.valor ?? ""]));
  const erro = h("div", { classe: "erro-form", hidden: true });
  const form = h("form", {
    classe: "formulario", onsubmit: async (ev) => {
      ev.preventDefault();
      erro.hidden = true;
      const v = lerForm(form);
      const valores = {}; const remover = [];
      for (const c of d.campos) {
        if (c.segredo) {
          if (v["rm_" + c.chave]) remover.push(c.chave);
          else if (v[c.chave]) valores[c.chave] = v[c.chave];
        } else if (v[c.chave] !== iniciais[c.chave]) {
          if (v[c.chave]) valores[c.chave] = v[c.chave]; else remover.push(c.chave);
        }
      }
      if (!Object.keys(valores).length && !remover.length) return toast("Nada foi alterado.");
      try {
        resultado(await api("/config", { valores, remover }));
        carregarListas();
        rerender();
      } catch (e) { erro.textContent = e.message; erro.hidden = false; }
    },
  },
  d.campos.map((c) => h("div", {},
    h("label", { classe: "campo" }, c.rotulo,
      h("input", {
        name: c.chave, type: c.segredo ? "password" : "text", value: c.segredo ? "" : c.valor, autocomplete: c.segredo ? "new-password" : "off",
        placeholder: c.segredo ? (c.definido ? "•••••••• definido (vazio mantém o atual)" : "não definido") : null,
      }), c.ajuda ? h("span", { classe: "ajuda" }, c.ajuda) : null),
    c.segredo && c.definido ? h("label", { classe: "marcar" }, h("input", { type: "checkbox", name: "rm_" + c.chave }), "Remover este valor") : null)),
  erro, h("div", {}, h("button", { type: "submit", classe: "primario" }, "Salvar configuração")));
  return [
    cabecalho("Configuração", botao("Testar Zabbix", () => iniciarTarefa("testar-zabbix")), botao("Testar TOPdesk", () => iniciarTarefa("testar-topdesk"))),
    h("p", { classe: "nota" }, "Arquivo ", h("code", {}, d.arquivo), ". Os segredos nunca são enviados ao navegador; a versão anterior fica em env.anterior."),
    !d.existe ? h("div", { classe: "alerta aviso" }, "O arquivo env ainda não existe: será criado ao salvar.") : null,
    h("div", { classe: "cartao" }, form),
  ];
};

Paginas.backups = async () => {
  const linhas = await api("/backups");
  return [
    cabecalho("Cópias de segurança", botao("Copiar o banco agora", () => acao(() => api("/backups", {}), rerender), "primario")),
    h("p", { classe: "nota" }, "As cópias ficam no próprio servidor: baixe para guardar fora dele. Cópias das definições são criadas sozinhas a cada edição."),
    h("div", { classe: "cartao" }, tabela([
      { titulo: "Arquivo", valor: (b) => h("a", { href: "/api/backup" + consulta({ nome: b.nome }) }, b.nome) },
      { titulo: "Tipo", valor: (b) => b.tipo },
      { titulo: "Criado em", classe: "nw", valor: (b) => b.criado_em },
      { titulo: "Tamanho", classe: "num", valor: (b) => tamanho(b.tamanho) },
      { titulo: "", valor: (b) => pequeno("Remover", () => confirmar({
        titulo: "Remover cópia", texto: b.nome, botaoTexto: "Remover", enviar: () => api("/backups/remover", { nome: b.nome }),
      }), "perigo") },
    ], linhas, { vazio: "Nenhuma cópia ainda." })),
  ];
};

Paginas.auditoria = async () => {
  const d = await api("/auditoria");
  const resumo = (o) => Object.entries(o || {}).filter(([, v]) => v !== null && v !== undefined && v !== "").map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(" ") : v}`).join(" · ");
  return [
    cabecalho("Auditoria"),
    h("div", { classe: "cartao" }, h("h2", {}, "Ações no painel"), h("p", { classe: "nota" }, "Últimos 500 registros. Este histórico não é apagado pela limpeza."), tabela([
      { titulo: "Quando", classe: "nw", valor: (a) => a.em },
      { titulo: "Quem", valor: (a) => a.usuario },
      { titulo: "Ação", classe: "nw", valor: (a) => a.acao },
      { titulo: "Detalhes", valor: (a) => resumo(a.detalhe) },
      { titulo: "IP", classe: "nw", valor: (a) => a.ip || "-" },
    ], d.painel, { vazio: "Nenhum registro." })),
    h("div", { classe: "cartao" }, h("h2", {}, "Alterações de expurgos no banco"), tabela([
      { titulo: "Quando", classe: "nw", valor: (a) => a.em },
      { titulo: "Tabela", valor: (a) => `${a.tabela} #${a.registro}` },
      { titulo: "Ação", valor: (a) => a.acao },
      { titulo: "Antes", valor: (a) => h("span", { classe: "mono" }, a.antes || "-") },
      { titulo: "Depois", valor: (a) => h("span", { classe: "mono" }, a.depois || "-") },
    ], d.banco, { vazio: "Nenhum registro." })),
  ];
};

Paginas.usuarios = async () => {
  const linhas = await api("/usuarios");
  const senha = (u) => abrirForm({
    titulo: `Trocar a senha de ${u.nome}`, botaoTexto: "Trocar senha",
    campos: [
      ...(u.voce ? [{ nome: "atual", rotulo: "Senha atual", tipo: "password", obrigatorio: true, autocomplete: "current-password" }] : []),
      { nome: "senha", rotulo: "Nova senha (mínimo de 10 caracteres)", tipo: "password", obrigatorio: true, autocomplete: "new-password" },
    ],
    enviar: (v) => api(`/usuarios/${u.nome}/senha`, v),
  });
  return [
    cabecalho("Usuários", botao("Novo usuário", () => abrirForm({
      titulo: "Novo usuário", botaoTexto: "Criar",
      nota: "Todos os usuários têm acesso total. O nome aparece nos expurgos e na auditoria.",
      campos: [
        { nome: "nome", rotulo: "Nome de usuário", obrigatorio: true },
        { nome: "senha", rotulo: "Senha (mínimo de 10 caracteres)", tipo: "password", obrigatorio: true, autocomplete: "new-password" },
      ],
      enviar: (v) => api("/usuarios", v),
    }), "primario")),
    h("div", { classe: "cartao" }, tabela([
      { titulo: "Usuário", valor: (u) => [u.nome, u.voce ? [" ", selo("você", "info")] : null] },
      { titulo: "Criado em", classe: "nw", valor: (u) => u.criado_em || "-" },
      { titulo: "Senha alterada em", classe: "nw", valor: (u) => u.senha_em || "-" },
      { titulo: "", valor: (u) => h("div", { classe: "acoes" },
        pequeno("Trocar senha", () => senha(u)),
        u.voce ? null : pequeno("Remover", () => confirmar({
          titulo: `Remover ${u.nome}`, texto: "O acesso é cortado na hora.", botaoTexto: "Remover", enviar: () => api(`/usuarios/${u.nome}/remover`, {}),
        }), "perigo")) },
    ], linhas)),
  ];
};

// ----------------------------------------------------------- estrutura ----

const MENU = [
  [null, [["painel", "Painel"]]],
  ["Operação", [["quedas", "Quedas"], ["expurgos", "Expurgos"], ["lacunas", "Lacunas"], ["itens", "Itens medidos"]]],
  ["Relatórios", [["relatorios", "Gerar relatório"], ["fechamento", "Fechamento"], ["pdfs", "Arquivos PDF"], ["definicoes", "Definições"]]],
  ["Sistema", [["tarefas", "Tarefas"], ["servicos", "Serviços e logs"], ["config", "Configuração"], ["backups", "Backups"], ["auditoria", "Auditoria"], ["usuarios", "Usuários"]]],
];

const rotaAtual = () => {
  const r = location.hash.slice(1);
  return Paginas[r] ? r : "painel";
};

let $conteudo = null;
let $lateral = null;

async function carregarListas() {
  LISTAS = await api("/listas");
}

function montarLateral() {
  const r = rotaAtual();
  $lateral.replaceChildren(...[
    h("div", { classe: "marca" }, h("span", {}, "ENGIE"), h("b", {}, "Disponibilidade")),
    MENU.map(([grupo, itens]) => h("div", { classe: "secao-nav" },
      grupo ? h("div", { classe: "grupo-nav" }, grupo) : null,
      itens.map(([id, rotulo]) => h("a", { classe: "nav" + (id === r ? " ativo" : ""), href: "#" + id, "aria-current": id === r ? "page" : null }, rotulo)))),
    h("div", { classe: "rodape-nav" },
      Object.entries(LISTAS.links).map(([k, url]) => h("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, `Abrir ${k}`)),
      h("div", {}, LISTAS.usuario),
      botao("Sair", async () => { await api("/logout", {}).catch(() => {}); telaLogin(); })),
  ].flat());
}

async function rerender() {
  if (!$conteudo) return;
  const n = ++renderAtual;
  const rota = rotaAtual();
  try {
    const nos = await Paginas[rota]();
    if (n !== renderAtual) return;
    $conteudo.replaceChildren(...[nos].flat(Infinity).filter(Boolean));
    document.title = `${MENU.flatMap((g) => g[1]).find((i) => i[0] === rota)[1]} - Relatórios ENGIE`;
  } catch (e) {
    if (n === renderAtual && e instanceof ErroApi && $conteudo) {
      $conteudo.replaceChildren(h("div", { classe: "alerta erro" }, e.message));
    } else if (!(e instanceof ErroApi)) { throw e; }
  }
}

async function telaPrincipal() {
  await carregarListas();
  $lateral = h("nav", { classe: "lateral", "aria-label": "Menu" });
  $conteudo = h("main");
  $app.replaceChildren(h("div", { classe: "layout" }, $lateral, $conteudo));
  montarLateral();
  await rerender();
}

function telaLogin(mensagem) {
  document.querySelectorAll("dialog").forEach((d) => d.close());
  $conteudo = null;
  const erro = h("div", { classe: "erro-form", hidden: !mensagem }, mensagem || "");
  const enviar = h("button", { type: "submit", classe: "primario" }, "Entrar");
  const form = h("form", {
    onsubmit: async (ev) => {
      ev.preventDefault();
      enviar.disabled = true;
      erro.hidden = true;
      try {
        await api("/login", lerForm(form));
        await telaPrincipal();
      } catch (e) {
        erro.textContent = e.message;
        erro.hidden = false;
      } finally { enviar.disabled = false; }
    },
  },
  h("div", { classe: "marca-login" }, "ENGIE"),
  h("h1", {}, "Relatórios de Disponibilidade"),
  campo({ nome: "usuario", rotulo: "Usuário", obrigatorio: true, autocomplete: "username" }),
  campo({ nome: "senha", rotulo: "Senha", tipo: "password", obrigatorio: true, autocomplete: "current-password" }),
  erro, enviar);
  $app.replaceChildren(h("div", { classe: "entrar" }, form));
  form.elements.usuario.focus();
}

window.addEventListener("hashchange", () => {
  if (!$conteudo) return;
  montarLateral();
  rerender();
  window.scrollTo(0, 0);
});

(async () => {
  try {
    const s = await api("/sessao");
    if (s.usuario) await telaPrincipal(); else telaLogin();
  } catch (e) { telaLogin(e.message); }
})();
