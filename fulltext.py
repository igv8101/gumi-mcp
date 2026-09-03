"""Texto para o índice de busca do Gumi-MCP (L5 — FTS5).

O que pode entrar no índice de conteúdo é decidido pela MESMA política das
tools (`security.py`): arquivo indexável, extensão de texto liberada, dentro
do teto de tamanho e sem cara de binário (byte nulo no começo — o sniff do
ripgrep). O conteúdo armazenado vive só em `data/gumi_state.db`; a zona
íntima nunca chega aqui porque a política barra antes.
"""

from pathlib import Path

from config import MAX_FILE_SIZE
from security import extensao_legivel, pode_indexar

# Quantos bytes do início são inspecionados atrás de byte nulo.
BYTES_DO_SNIFF = 8_192

# Teto do trecho devolvido pela busca — linha minificada não vira resposta gigante.
MAX_TRECHO = 240


def texto_para_indexar(caminho: Path) -> str | None:
    """Conteúdo textual de um arquivo, ou None se ele não entra na busca.

    None não é erro: binário, grande demais, extensão fora da lista e caminho
    bloqueado pela política simplesmente não têm conteúdo indexável.
    """
    if not pode_indexar(caminho) or not extensao_legivel(caminho):
        return None

    try:
        if not caminho.is_file() or caminho.stat().st_size > MAX_FILE_SIZE:
            return None
        with caminho.open("rb") as arquivo:
            inicio = arquivo.read(BYTES_DO_SNIFF)
            if b"\x00" in inicio:
                return None
            resto = arquivo.read()
    except OSError:
        return None

    return (inicio + resto).decode("utf-8", errors="replace")


def frase_para_match(consulta: str) -> str:
    """Consulta literal vira frase FTS5.

    As aspas neutralizam a sintaxe da MATCH (AND, OR, NEAR, *) — o que o
    agente digita é texto procurado, nunca operador.
    """
    return '"' + consulta.replace('"', '""') + '"'


def termos_da_consulta(consulta: str) -> list[str]:
    """Termos na mesma quebra do tokenizador unicode61 (letras e dígitos).

    Lista vazia = consulta só de pontuação: o FTS não tem o que casar e a
    busca cai para a varredura literal.
    """
    termos: list[str] = []
    atual: list[str] = []
    for caractere in consulta.casefold():
        if caractere.isalnum():
            atual.append(caractere)
        elif atual:
            termos.append("".join(atual))
            atual = []
    if atual:
        termos.append("".join(atual))
    return termos


def localizar_trecho(conteudo: str, consulta: str) -> tuple[int | None, str | None]:
    """Primeira linha onde a consulta aparece, e o trecho recortado dela.

    Tenta a consulta inteira (casefold); se o FTS casou por token, tenta
    termo a termo. Devolve (None, None) se nada aparecer — quem chama
    decide mostrar só o caminho.
    """
    alvos = [consulta.casefold()]
    alvos += [termo for termo in termos_da_consulta(consulta) if termo not in alvos]

    linhas = conteudo.splitlines()
    for alvo in alvos:
        if not alvo:
            continue
        for numero, linha in enumerate(linhas, start=1):
            posicao = linha.casefold().find(alvo)
            if posicao >= 0:
                return numero, _recortar(linha, posicao, len(alvo))
    return None, None


def _recortar(linha: str, posicao: int, tamanho: int) -> str:
    """Janela de MAX_TRECHO caracteres centrada na ocorrência."""
    if len(linha) <= MAX_TRECHO:
        return linha.strip()

    inicio = max(0, posicao - (MAX_TRECHO - tamanho) // 2)
    fim = min(len(linha), inicio + MAX_TRECHO)
    prefixo = "…" if inicio > 0 else ""
    sufixo = "…" if fim < len(linha) else ""
    return prefixo + linha[inicio:fim].strip() + sufixo
