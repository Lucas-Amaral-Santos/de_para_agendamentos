import streamlit as st
import pandas as pd
import mysql.connector
import unicodedata
from datetime import datetime
from io import StringIO, BytesIO
import numpy as np


# FUNÇÕES AUXILIARES
# =====================================================================
def norm_nome(s):
    """Normaliza nomes para servirem de CHAVE de cruzamento entre as bases.

    O join é feito por nome (a planilha da Duda não traz um ID comum ao
    banco), e a mesma pessoa costuma vir grafada de formas diferentes
    ("João" x "JOAO", espaços a mais, acentos). Padronizamos para
    MAIÚSCULAS, sem acento e com espaços colapsados, senão o mesmo
    paciente não casa entre as duas fontes.
    """
    if pd.isna(s):
        return s
    s = str(s).strip().upper()
    s = "".join(c for c in unicodedata.normalize("NFKD", s)
                if not unicodedata.combining(c))
    return " ".join(s.split())

def eh_dia(c):
    """Diz se o nome da coluna é um dia do mês (1..31).

    Precisa ser robusto porque o read_excel às vezes traz o cabeçalho dos
    dias como inteiro (1), string ("1") ou float (1.0) — e "1.0".isdigit()
    é False. Convertendo para float e checando o intervalo, os três casos
    passam e colunas como 'Usuário' ficam de fora.
    """
    try:
        v = float(str(c).strip())
        return v.is_integer() and 1 <= v <= 31
    except (ValueError, TypeError):
        return False

def junta_justificativas(s):
    """Junta as justificativas de um mesmo (atendido, dia).

    O banco pode ter mais de um registro no mesmo dia (mais de um setor/
    agendamento). Ao agrupar por dia, concatenamos as justificativas
    únicas, preservando a ordem, para não perder informação nem repetir.
    """
    vals = [str(x).strip() for x in s.dropna() if str(x).strip()]
    return " | ".join(dict.fromkeys(vals))

st.markdown("# DE-PARA AGENDAMENTOS FINANCEIRO")

def get_mes_index(mes_str):
    """Retorna o índice do mês (1..12) dado o nome do mês em português."""
    meses = ["Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
             "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"]
    try:
        return meses.index(mes_str) + 1
    except ValueError:
        return None

meses_options = ["Janeiro", "Fevereiro", "Março", "Abril", 
                 "Maio", "Junho", "Julho", "Agosto", "Setembro", 
                 "Outubro", "Novembro", "Dezembro"]

mes_input = st.selectbox("Mês:", meses_options)

ano_input = int(st.number_input("Ano:", value=datetime.now().year, min_value=2000, max_value=2100))

MES = get_mes_index(mes_input)
ANO = ano_input

conn = mysql.connector.connect(
    host="mysql20-farm1.kinghost.net",
    user="afr0202_add1",
    password="La12345",
    database="afr02",
    port=3306
)

df = pd.read_sql(
            f"SELECT `Data`, `Atendido`, `Setor`, `Falta`, `Justificativa` FROM agendamentos WHERE MONTH(`Data`)={MES} AND YEAR(`Data`)={ANO}", conn
        )

st.dataframe(df)

uploaded_file = st.file_uploader("Anexe a planilha")
if uploaded_file is not None:

    df_2 = pd.read_excel(uploaded_file, skiprows=2)
    df_2 = df_2.drop(columns=["Unnamed: 0", "Nº"])

    MAP = {"P": "PRESENCA", "F": "FALTA", "D": "DISPENSA"}
    dias = [c for c in df_2.columns if eh_dia(c)]


    wide_long = (
        df_2.melt(                                   # "achata" as colunas de dia
            id_vars=["Usuário", "Prontuário"],
            value_vars=dias,
            var_name="dia", value_name="marca",
        )
        .dropna(subset=["marca"])                    # NaN = sem atendimento -> descarta
    )
    wide_long["dia"]   = wide_long["dia"].astype(float).astype(int)
    wide_long["Data"]  = pd.to_datetime(
        dict(year=ANO, month=MES, day=wide_long["dia"])).dt.date
    wide_long["marca"] = wide_long["marca"].astype(str).str.strip().str.upper()
    wide_long["status"]      = wide_long["marca"].map(MAP).fillna("DESCONHECIDO")
    wide_long["faltou_duda"] = wide_long["status"].eq("FALTA")
    wide_long["nome_key"]    = wide_long["Usuário"].map(norm_nome)

    wide_long = wide_long.sort_values(["Usuário", "dia"]).reset_index(drop=True)

    banco = df.copy()
    banco["Data"]     = pd.to_datetime(banco["Data"]).dt.date
    banco["nome_key"] = banco["Atendido"].map(norm_nome)
    banco["faltou"]   = banco["Falta"].astype(str).str.strip().str.upper().eq("FALTA")

    # Mantém apenas os atendidos que existem no relatório da Duda: a conferência
    # é sobre os pacientes DELA; sem este filtro, todo o resto do banco entraria.
    banco = banco[banco["nome_key"].isin(set(wide_long["nome_key"]))]

    mensal = (
        banco.groupby(["nome_key", "Atendido", "Data"], as_index=False)
            .agg(faltou_mensal=("faltou", "max"),           # faltou se qualquer registro do dia for falta
                justificativa=("Justificativa", junta_justificativas))
    )
    mensal = mensal.sort_values(["Atendido", "Data"]).reset_index(drop=True)


    depara = wide_long.merge(
        mensal, on=["nome_key", "Data"], how="outer", suffixes=("", "_m"),
    )
    # dia sem registro de um dos lados: por aquele lado, "não faltou".
    depara["faltou_duda"]   = depara["faltou_duda"].fillna(False)
    depara["faltou_mensal"] = depara["faltou_mensal"].fillna(False)

    # nome e prontuário legíveis mesmo nas linhas que vieram só do banco
    depara["Nome"] = depara["Usuário"].fillna(depara["Atendido"])
    pront = wide_long.drop_duplicates("nome_key").set_index("nome_key")["Prontuário"]
    depara["Prontuário"] = depara["Prontuário"].fillna(depara["nome_key"].map(pront))

    # fica só onde o "faltou" diverge entre as bases
    div = depara[depara["faltou_duda"] != depara["faltou_mensal"]].copy()
    div["onde"] = np.where(
        div["faltou_duda"], "falta só na Duda", "falta só no mensal")

    div = (
        div[["Nome", "Prontuário", "Data",
            "faltou_duda", "faltou_mensal", "onde", "justificativa"]]
        .sort_values(["Nome", "Data"])
        .reset_index(drop=True)
    )


    st.dataframe(div)

    # Gerar arquivo Excel com divergências
    buffer = BytesIO()
    div.to_excel(buffer, index=False, engine='openpyxl')
    buffer.seek(0)

    st.download_button(
        label="Download do DE-PARA",
        data=buffer,
        file_name="divergencias_faltas.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
