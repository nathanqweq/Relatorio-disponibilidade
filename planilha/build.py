from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.formatting.rule import CellIsRule, FormulaRule
from openpyxl.utils import get_column_letter

F="Arial"
H_FILL=PatternFill("solid", fgColor="0B2E4F"); H_FONT=Font(name=F,bold=True,color="FFFFFF",size=10)
B=Font(name=F,size=10); BOLD=Font(name=F,size=10,bold=True)
IN_FILL=PatternFill("solid", fgColor="FFF2CC"); BLUE=Font(name=F,size=10,color="0000FF")
thin=Side(style="thin",color="D0D7DE"); BOX=Border(left=thin,right=thin,top=thin,bottom=thin)
WRAP=Alignment(wrap_text=True,vertical="top"); CENTER=Alignment(horizontal="center",vertical="center",wrap_text=True)

def table(ws, r0, headers, rows, widths):
    for j,h in enumerate(headers,1):
        c=ws.cell(r0,j,h); c.fill=H_FILL; c.font=H_FONT; c.alignment=CENTER; c.border=BOX
    for i,row in enumerate(rows,r0+1):
        for j,v in enumerate(row,1):
            c=ws.cell(i,j,v); c.font=B; c.alignment=WRAP; c.border=BOX
    for j,w in enumerate(widths,1): ws.column_dimensions[get_column_letter(j)].width=w
    ws.freeze_panes=ws.cell(r0+1,1)
    return r0+len(rows)

def title(ws, t, sub):
    ws["A1"]=t; ws["A1"].font=Font(name=F,size=14,bold=True,color="0B2E4F")
    ws["A2"]=sub; ws["A2"].font=Font(name=F,size=10,italic=True,color="5F6B7A")

wb=Workbook()

# ---------- Blocos
ws=wb.active; ws.title="Blocos"
title(ws,"Blocos de relatório","Os 7 tipos de bloco que compõem os 10 relatórios mensais da ENGIE")
blocos=[
 (1,"Disponibilidade geral","Um percentual com mês e ano","Disponibilidade Geral TID; Link Internet Algar; Link BGP Embratel","Eventos do Zabbix (SQLite)"),
 (2,"Tabela de disponibilidade por item","Lista de itens com o percentual de cada um","Usinas; links por operadora; neighbors BGP; firewalls","Eventos do Zabbix (SQLite)"),
 (3,"Contador","Um número em destaque","Acionamentos de operadora (49); atendimentos no mês (5.263); backlog (0); SLAs violados (0); cases POWERPLANT (14)","Chamados (TOPdesk)"),
 (4,"Tabela de chamados","Número, chamado externo, descrição, down, up, tempo fora e causa","Acionamento de operadoras; cases POWERPLANT","Chamados (TOPdesk) + eventos do Zabbix"),
 (5,"Top 10","Gráfico de barras e tabela de contagem por tipo de incidente","Top 10 incidentes; Top 10 links MPLS","Chamados (TOPdesk)"),
 (6,"Evidência por item abaixo de 100%","Nota explicativa, chamados ligados, gráfico e eventos do período","Evidências dos links da Oi; evidências de disponibilidade da UHSJ","Eventos do Zabbix + ação registrada no chamado (TOPdesk)"),
 (7,"Texto e inventário","Contatos, dispositivos por tipo, regras de correlação, recomendações e itens de melhoria","Contatos; dispositivos por tipo; correlações da Teleassistência; itens de melhoria","API do Zabbix (inventário) + texto mensal (não implementado)"),
]
end=table(ws,4,["Nº","Bloco","O que mostra","Exemplos nos relatórios atuais","Fonte de dados"],blocos,[6,32,44,60,40])
ws.cell(end+2,1,"Uso do bloco nos relatórios: contagem a partir da aba Matriz.").font=Font(name=F,size=9,italic=True,color="5F6B7A")

# ---------- Relatórios
ws=wb.create_sheet("Relatorios")
title(ws,"Relatórios mensais","Os 10 relatórios de julho de 2026 analisados")
rels=[
 ("Disponibilidade Geral TID","ENGIE_-_Disponibilidade_Geral_Julho_2026-_TID_V2.pdf",1,"1, 2","Eventos do Zabbix","Usinas, transmissão e aplicações"),
 ("Disponibilidade Switches Core GA e NE","ENGIE_-_Disponibilidade_Switches_Core_GA_e_NE_-_Julho_2026_v0.pdf",1,"1, 2","Eventos do Zabbix","Versão resumida (SolarWinds)"),
 ("Relatório Mensal Switches Core GA e NE","Engie_Report_Mensal_Julho_de_2026_Switches_Core_GA_e_NE.pdf",6,"1, 2, 6","Eventos do Zabbix","Versão mensal com evidências"),
 ("Links Internet (operadoras)","ENGIE_LINKS_-_Disponibilidade_Julho_2026_V0.pdf",2,"1, 2","Eventos do Zabbix","Bloco 1 repetido por operadora"),
 ("Links BGP","ENGIE_LINKS_BGP_-_Disponibilidade_Julho_2026.pdf",1,"1, 2","Eventos do Zabbix","Embratel e Oi"),
 ("Firewalls SCII (página única)","Disponibilidade_Mensal_ENGIE_-_EBE_OPE_-_FIREWALLS_SCII_Julho_2026_v1.pdf",1,"2","Eventos do Zabbix",""),
 ("Relatório Mensal Firewalls SCI","Engie_Report_Mensal_Julho_de_2026_FIREWALLS_SCI.pdf",10,"2, 6, 7","Eventos do Zabbix + texto",""),
 ("Relatório Mensal Operadoras","Engie_Report_Mensal_Julho_de_2026_-_Disponibilidades_Operadoras.pdf",199,"1, 2, 3, 4, 5, 6","Zabbix + chamados","Blocos 1, 2 e 6 por operadora; Top 10 de MPLS"),
 ("Relatório Mensal EBE","Engie_Report_Mensal_Julho_de_2026_EBE.pdf",194,"1, 2, 3, 4, 5, 6, 7","Zabbix + chamados + inventário + texto","Blocos repetidos por torre (Servidores, Redes)"),
 ("Relatório Mensal Teleassistência","Engie_Report_Mensal_Teleassistência_Julho.pdf",41,"3, 5, 7","Chamados + texto","Inclui regras de correlação"),
]
end=table(ws,4,["Relatório","Arquivo de referência","Páginas (jul/26)","Blocos usados","Fontes de dados","Observações"],rels,[38,58,14,18,32,40])
for i in range(5,end+1):
    ws.cell(i,3).alignment=CENTER; ws.cell(i,4).alignment=CENTER
r=end+1
ws.cell(r,1,"Total de páginas").font=BOLD
ws.cell(r,3,f"=SUM(C5:C{end})").font=BOLD; ws.cell(r,3).alignment=CENTER
ws.cell(r+1,1,"Páginas contadas nos PDFs de julho de 2026 enviados pelo usuário.").font=Font(name=F,size=9,italic=True,color="5F6B7A")

# ---------- Matriz
ws=wb.create_sheet("Matriz")
title(ws,"Matriz relatório × bloco","X = o relatório usa o bloco; as contagens se atualizam ao marcar ou desmarcar")
nomes=[x[1] for x in blocos]
hdr=["Relatório"]+[f"{b[0]}. {b[1]}" for b in blocos]+["Qtd. de blocos"]
for j,h in enumerate(hdr,1):
    c=ws.cell(4,j,h); c.fill=H_FILL; c.font=H_FONT; c.alignment=CENTER; c.border=BOX
ws.row_dimensions[4].height=48
dv=DataValidation(type="list",formula1='"X"',allow_blank=True); ws.add_data_validation(dv)
for i,rel in enumerate(rels,5):
    ws.cell(i,1,f"=Relatorios!A{i}").font=Font(name=F,size=10,color="008000"); ws.cell(i,1).border=BOX
    usados={int(x) for x in rel[3].split(",")}
    for k in range(1,8):
        c=ws.cell(i,1+k,"X" if k in usados else None); c.font=BLUE; c.alignment=CENTER; c.border=BOX; c.fill=IN_FILL
        dv.add(c)
    c=ws.cell(i,9,f'=COUNTIF(B{i}:H{i},"X")'); c.font=BOLD; c.alignment=CENTER; c.border=BOX
last=4+len(rels)
ws.cell(last+1,1,"Relatórios que usam o bloco").font=BOLD
for k in range(2,9):
    col=get_column_letter(k)
    c=ws.cell(last+1,k,f'=COUNTIF({col}5:{col}{last},"X")'); c.font=BOLD; c.alignment=CENTER; c.border=BOX
ws.column_dimensions["A"].width=40
for k in range(2,9): ws.column_dimensions[get_column_letter(k)].width=17
ws.column_dimensions["I"].width=14
ws.freeze_panes="B5"
ws.cell(last+3,1,"Células amarelas são editáveis: escolha X na lista ou deixe em branco.").font=Font(name=F,size=9,italic=True,color="5F6B7A")
# contagem de uso na aba Blocos
wb["Blocos"].cell(4,6,"Usado em (relatórios)").fill=H_FILL; wb["Blocos"].cell(4,6).font=H_FONT; wb["Blocos"].cell(4,6).alignment=CENTER; wb["Blocos"].cell(4,6).border=BOX
for i in range(7):
    c=wb["Blocos"].cell(5+i,6,f"=Matriz!{get_column_letter(2+i)}{last+1}"); c.font=Font(name=F,size=10,color="008000"); c.alignment=CENTER; c.border=BOX
wb["Blocos"].column_dimensions["F"].width=20

# ---------- Fontes
ws=wb.create_sheet("Fontes")
title(ws,"Fontes de dados","De onde cada tipo de informação será coletado")
fontes=[
 ("Eventos do Zabbix → SQLite","1, 2, 6","atualizar.sh importa pela API os problemas das triggers com a tag relatorio (início, fim, tags, chamado)","Base de todos os percentuais de disponibilidade"),
 ("Chamados (TOPdesk)","3, 4, 5","API do TOPdesk, gravada no mesmo banco","Nos relatórios atuais esses dados vêm do ServiceNow da Vision"),
 ("Inventário","7","API do Zabbix","Dispositivos por tipo"),
 ("Texto mensal","7","Não implementado (tabela textos_mensais pronta no banco)","Recomendações e itens de melhoria"),
 ("Resumo do chamado","6","Última ação registrada pelo analista no TOPdesk","Notas de evidência por item abaixo da meta"),
]
table(ws,4,["Fonte","Blocos","Como coletar","Observação"],fontes,[30,12,62,52])

# ---------- Tags
ws=wb.create_sheet("Tags")
title(ws,"Tags no Zabbix","Classificação que permite incluir métricas novas sem mudar código")
tags=[
 ("relatorio","links-internet","Define em qual relatório o problema entra; o coletor grava todo problema resolvido que tenha esta tag"),
 ("relatorio","firewalls-sci","Exemplo para o relatório de Firewalls SCI"),
 ("operadora","OI","Agrupa por operadora nos relatórios de links"),
 ("item","UHSA_MON_INTERNET_OI","Identifica o item exibido na tabela de disponibilidade"),
]
end=table(ws,4,["Tag","Valor de exemplo","Para que serve"],tags,[18,28,90])
ws.cell(end+2,1,"Nova métrica = colocar as tags na trigger + adicionar uma linha no YAML do relatório. Nenhuma linha de código muda.").font=Font(name=F,size=10,italic=True)

# ---------- Plano
ws=wb.create_sheet("Plano")
title(ws,"Plano de implementação","Ordem sugerida; atualize a coluna Status conforme o andamento")
plano=[
 (1,"Motor de disponibilidade por eventos + tags + relatório TID de ponta a ponta","Validar o cálculo contra os números de julho de 2026","Disponibilidade Geral TID","Não iniciado"),
 (2,"Links Internet, Links BGP e Firewalls SCI","Mesmos blocos, com agrupamento por operadora","Links Internet; Links BGP; Firewalls SCII; Relatório Mensal Firewalls SCI","Não iniciado"),
 (3,"Blocos de chamados: contadores, tabela de acionamentos e Top 10","Exige integração com o TOPdesk","—","Não iniciado"),
 (4,"Relatórios grandes por composição dos blocos","Apenas combinação do que já existe","Operadoras; EBE; Teleassistência; Switches Core GA e NE","Não iniciado"),
]
end=table(ws,4,["Fase","Entrega","Observação","Relatórios atendidos","Status"],plano,[8,58,44,52,16])
dv2=DataValidation(type="list",formula1='"Não iniciado,Em andamento,Concluído"',allow_blank=False); ws.add_data_validation(dv2)
for i in range(5,end+1):
    c=ws.cell(i,5); c.fill=IN_FILL; c.font=BLUE; c.alignment=CENTER; dv2.add(c)
    ws.cell(i,1).alignment=CENTER
ws.conditional_formatting.add(f"E5:E{end}",CellIsRule(operator="equal",formula=['"Concluído"'],fill=PatternFill("solid",fgColor="D9EAD3")))
ws.conditional_formatting.add(f"E5:E{end}",CellIsRule(operator="equal",formula=['"Em andamento"'],fill=PatternFill("solid",fgColor="FCE5CD")))
r=end+2
ws.cell(r,2,"Fases concluídas").font=BOLD
ws.cell(r,5,f'=COUNTIF(E5:E{end},"Concluído")&" de "&COUNTA(E5:E{end})').font=BOLD; ws.cell(r,5).alignment=CENTER
ws.cell(r+1,2,"Coluna Status (amarela) é editável.").font=Font(name=F,size=9,italic=True,color="5F6B7A")

# ---------- Pendências
ws=wb.create_sheet("Pendencias")
title(ws,"Pendências","Decisões em aberto")
pend=[(1,"O chamado do incidente será sempre o do TOPdesk, ou o chamado da operadora entra como campo separado (External Ticket)?","Define as colunas da tabela de incidentes no banco","Aberta")]
end=table(ws,4,["Nº","Pergunta","Impacto","Situação"],pend,[6,80,44,14])
ws.cell(5,4).fill=IN_FILL; ws.cell(5,4).font=BLUE

for s in wb.worksheets:
    s.sheet_view.showGridLines=False
wb.save("ENGIE_Relatorios_Blocos_e_Plano.xlsx")
