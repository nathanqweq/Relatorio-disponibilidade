from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.formatting.rule import CellIsRule
from openpyxl.utils import get_column_letter
F="Arial"
H_FILL=PatternFill("solid", fgColor="0B2E4F"); H_FONT=Font(name=F,bold=True,color="FFFFFF",size=10)
B=Font(name=F,size=10); MONO=Font(name="Consolas",size=10); BOLD=Font(name=F,size=10,bold=True)
IN_FILL=PatternFill("solid", fgColor="FFF2CC"); BLUE=Font(name=F,size=10,color="0000FF")
thin=Side(style="thin",color="D0D7DE"); BOX=Border(left=thin,right=thin,top=thin,bottom=thin)
WRAP=Alignment(wrap_text=True,vertical="top"); CENTER=Alignment(horizontal="center",vertical="center",wrap_text=True)
QUIET=Font(name=F,size=9,italic=True,color="5F6B7A")

wb=load_workbook("ENGIE_Relatorios_Blocos_e_Plano.xlsx")
if "Guia_Tags" in wb.sheetnames: del wb["Guia_Tags"]
ws=wb.create_sheet("Guia_Tags", 1)
ws["A1"]="Guia de tags por relatório"; ws["A1"].font=Font(name=F,size=14,bold=True,color="0B2E4F")
ws["A2"]="Tags a colocar nas triggers do Zabbix que definem indisponibilidade. Coluna \"Trigger\" é sugestão a partir dos PDFs de julho/2026: confirmar o nome real no Zabbix."; ws["A2"].font=QUIET

hdr=["Chave (tag relatorio)","Relatório(s) atendidos","O que conta como indisponível","Trigger no Zabbix (a confirmar)",
     "Tag grupo","Tag item","Itens no PDF de jul/26","Exemplos de itens","Observação","Status"]
rows=[
 ("tid-usinas","Disponibilidade Geral TID; EBE (seções 2 a 4)","Usina de geração fora (case \"Usina / Escritório Down\")",
  "Trigger de usina down no host \"<SIGLA> - Core Status\"","GERACAO","<SIGLA> (ex.: UHSJ)","29",
  "UHSJ, ASSU, CEAR, CECL, UHMI, SEDE_OFFICE","Uma trigger por usina: o grupo inteiro fora conta como indisponível",""),
 ("tid-transmissao","Disponibilidade Geral TID; EBE","Subestação de transmissão fora",
  "Trigger de usina down no host \"<SIGLA> - Core Status\"","TRANSMISSAO","<SIGLA> (ex.: AISE)","20",
  "AISE, ABP3, CPEN, GAAR, NEIT, TENE","As duas TENE precisam de item diferente (ex.: TENE-SERRA-PELADA e TENE-XINGU)",""),
 ("tid-aplicacoes","Disponibilidade Geral TID; EBE (seção 5)","Componente da aplicação fora (porta TCP, cenário web, processo)",
  "Triggers dos itens net.tcp.service e web.test.fail nos hosts \"ENGIE APP - TID *\"","{HOST.NAME}","<APP> - <componente> (ex.: TID-JAVA - TCP 8080 JbAPPro1)","9 aplicações / 54 componentes",
  "TID-EBS, TID-Forms, TID-JAVA, TID Banco GEN","Item escrito em cada trigger, pois cada componente é uma linha própria",""),
 ("links-internet","Links Internet; Operadoras (seções 1 a 11)","Link de internet fora",
  "\"IPSLA Internet DOWN / Alta latência\" (template de IP SLA)","Operadora: ALGAR, OI, BRDIGITAL, INTERJATO, EMBRATEL, NETINHO, MHNET, WIRELINK, INTEGRATO, OUTRAS","{HOST.NAME}","~80 (nomes cortados no PDF)",
  "UHSA_MON_INTERNET_OI, EBSE_MON_INTERNET_ALGAR","Decidir se alta latência conta como indisponível; se não, separar as triggers",""),
 ("links-mpls","Operadoras (Relatório Disponibilidades Links MPLS)","Link MPLS fora",
  "Interface / IP SLA MPLS down (confirmar trigger)","Operadora: EMBRATEL, OI","{HOST.NAME}","—",
  "SD_ONS-NE_SAL (MPLS OI)","Top 10 de MPLS vem dos chamados, não desta tag",""),
 ("links-bgp","Links BGP","Sessão BGP fora",
  "\"BGP - Neighbor is down\" (protótipo de LLD por neighbor)","Operadora: EMBRATEL, OI","{HOST.NAME} - <operadora> (ex.: SD_GACN_ADM - OI)","14 Embratel / 29 Oi",
  "SD_UHSO_ADM, SD_GAGV_OPE, SD_CETR_PECEM_OPE","O mesmo host aparece nas duas operadoras: o item precisa incluir a operadora para não colidir",""),
 ("switches-core","Switches Core GA e NE (as duas versões)","Switch core da usina fora",
  "\"NODE DOWN\" dos switches core (ex.: SW_CECL-REGI_CORE)","GA ou NE","<SIGLA>_POWERPLANT (ex.: GAPG)","10 GA / 4 NE",
  "GAPG, GAPS, GAUV, NESP, NEIT, NEXI","Várias triggers no mesmo item = qualquer uma fora conta. Com switches redundantes, usar uma trigger agregada",""),
 ("firewalls-sci","Firewalls SCII; Relatório Mensal Firewalls SCI","Firewall SCI (VIP) fora",
  "\"NODE DOWN\" do host do firewall VIP","GA, NE ou geração (opcional)","{HOST.NAME}","36",
  "FW_UHSJ_SCI_VIP, FW_GAIR_SCI_VIP, FW_SEDE_SCI_VIP","",""),
 ("teleassistencia","Teleassistência","Conjunto de equipamentos da localidade fora (regras de correlação)",
  "\"Tele Assistência ... DOWN\": GTW, Switches SCI, Firewalls SCI, Switches Core, links de comunicação","Tipo: GTW, SWITCHES-SCI, FIREWALLS-SCI, SWITCHES-CORE, LINKS","<SIGLA> (ex.: NEIT)","65 incidentes no Top 10",
  "GTW NEIT DOWN, GTW CFFL DOWN","Relatório mostra contagem de incidentes; a tag serve para contar e agrupar",""),
 ("(sem tag)","EBE (Servidores e Redes); Operadoras (acionamentos, Top 10)","—",
  "—","—","—","—",
  "Backlog, atendimentos no mês, Top 10, SLAs violados","Vêm dos chamados do TOPdesk, não de triggers",""),
]
for j,h in enumerate(hdr,1):
    c=ws.cell(4,j,h); c.fill=H_FILL; c.font=H_FONT; c.alignment=CENTER; c.border=BOX
ws.row_dimensions[4].height=32
widths=[18,30,30,40,28,30,14,32,44,15]
for j,w in enumerate(widths,1): ws.column_dimensions[get_column_letter(j)].width=w
dv=DataValidation(type="list",formula1='"Não iniciado,Em andamento,Tags aplicadas,Validado"',allow_blank=False); ws.add_data_validation(dv)
for i,r in enumerate(rows,5):
    for j,v in enumerate(r,1):
        c=ws.cell(i,j,v); c.border=BOX; c.alignment=WRAP
        c.font=MONO if j in (1,6) else B
    ws.cell(i,7).alignment=CENTER
    s=ws.cell(i,10)
    if r[0]=="(sem tag)":
        s.value="—"; s.alignment=CENTER; s.font=B
    else:
        s.value="Não iniciado"; s.fill=IN_FILL; s.font=BLUE; s.alignment=CENTER; dv.add(s)
last=4+len(rows)
ws.conditional_formatting.add(f"J5:J{last}",CellIsRule(operator="equal",formula=['"Validado"'],fill=PatternFill("solid",fgColor="D9EAD3")))
ws.conditional_formatting.add(f"J5:J{last}",CellIsRule(operator="equal",formula=['"Tags aplicadas"'],fill=PatternFill("solid",fgColor="CFE2F3")))
ws.conditional_formatting.add(f"J5:J{last}",CellIsRule(operator="equal",formula=['"Em andamento"'],fill=PatternFill("solid",fgColor="FCE5CD")))
ws.freeze_panes="B5"
r=last+1
ws.cell(r,1,"Validados").font=BOLD
ws.cell(r,10,f'=COUNTIF(J5:J{last},"Validado")&" de "&COUNTIF(J5:J{last},"<>—")').font=BOLD; ws.cell(r,10).alignment=CENTER

r+=2
ws.cell(r,1,"Regras").font=Font(name=F,size=12,bold=True,color="0B2E4F")
regras=[
 "1. A tag relatorio pode se repetir na mesma trigger para o item entrar em mais de um relatório (ex.: tid-usinas e switches-core).",
 "2. O valor de item precisa ser único dentro do relatório: dois itens iguais viram uma linha só.",
 "3. Várias triggers com o mesmo relatorio + item formam um único objeto: basta uma delas em problema para contar como fora.",
 "4. Para equipamentos redundantes (só conta fora se todos caírem), crie uma trigger agregada e coloque a tag só nela.",
 "5. Em templates, use {HOST.NAME} ou {HOST.HOST} no valor; em protótipos de LLD, as macros {#...} são resolvidas na descoberta.",
 "6. Valores exatos e em minúsculas na chave do relatorio (links-internet, não Links Internet): é a chave no banco.",
 "7. Validado = rodar sync-objetos + importar-eventos + calcular e comparar com o PDF do mês anterior.",
]
for k,t in enumerate(regras, r+1):
    ws.cell(k,1,t).font=B
    ws.merge_cells(start_row=k,start_column=1,end_row=k,end_column=9)
    ws.cell(k,1).alignment=Alignment(wrap_text=True,vertical="top")
ws.sheet_view.showGridLines=False
ws.cell(2,1).alignment=Alignment(wrap_text=False)
wb.active=1
wb.save("ENGIE_Relatorios_Blocos_e_Plano.xlsx")
