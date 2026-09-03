"""Política única de acesso do Gumi-MCP.

Toda tool passa por aqui — nenhuma toca em `Path` por conta própria. São três
perguntas, sempre nesta ordem:

    1. o caminho está dentro do workspace?     -> `resolver`
    2. é uma pasta estrutural sem interesse?   -> `esta_ignorado`
    3. é zona íntima da Gumi?                  -> `motivo_privacidade`

O que a política protege não é o servidor: é a pessoa dona do
workspace. Leitura via MCP sai da
máquina, então "na dúvida, bloqueia".
"""

import os
from fnmatch import fnmatch
from pathlib import Path

from config import (
    GUMI_ROOT,
    IGNORE_DIRECTORIES,
    PRIVATE_DIRECTORIES,
    PRIVATE_DIRECTORY_PREFIXES,
    PRIVATE_FILE_PATTERNS,
    READABLE_EXTENSIONS,
)


class AcessoNegado(PermissionError):
    """Bloqueio deliberado da política — não é um erro do sistema de arquivos."""


class ForaDoWorkspace(AcessoNegado):
    """O caminho pedido cai fora da raiz autorizada."""


# ============================================================
# 1. O caminho está dentro do workspace?
# ============================================================

def resolver(relative_path: str = ".") -> Path:
    """Converte um caminho relativo em absoluto, preso ao workspace.

    `resolve()` normaliza `..` e segue symlinks ANTES da checagem, então nem
    `../../segredo` nem um atalho apontando para fora passam.
    """
    if relative_path is None:
        relative_path = "."

    texto = str(relative_path).strip()
    if texto in ("", "."):
        return GUMI_ROOT

    alvo = (GUMI_ROOT / texto).resolve()

    try:
        alvo.relative_to(GUMI_ROOT)
    except ValueError:
        raise ForaDoWorkspace(
            f"Caminho fora do workspace da Gumi: {relative_path!r}. "
            f"Use caminhos relativos à raiz (ex.: 'gumi/servicos')."
        ) from None

    return alvo


def caminho_relativo(alvo: Path) -> str:
    """Caminho em relação à raiz, sempre com '/' — o mesmo texto que vai ao banco."""
    return alvo.relative_to(GUMI_ROOT).as_posix()


# ============================================================
# 2. É pasta estrutural sem interesse?
# ============================================================

def esta_ignorado(alvo: Path) -> bool:
    """True se qualquer parte do caminho for uma pasta gerada/de ferramenta."""
    return any(
        parte.casefold() in IGNORE_DIRECTORIES
        for parte in _partes(alvo)
    )


# ============================================================
# 3. É zona íntima da Gumi?
# ============================================================

def motivo_privacidade(alvo: Path) -> str | None:
    """Devolve o motivo do bloqueio, ou None se o caminho é liberado.

    A mensagem é para o modelo ler: diz o que foi bloqueado e por quê, para que
    ele não fique tentando outra tool no mesmo lugar.
    """
    partes = _partes(alvo)

    for parte in partes:
        nome = parte.casefold()

        if nome in PRIVATE_DIRECTORIES:
            return (
                f"a pasta '{parte}' guarda dados pessoais "
                f"(memórias, conversas, diário, saúde, mídia) e não sai da máquina"
            )

        if nome.startswith(PRIVATE_DIRECTORY_PREFIXES):
            return f"a pasta '{parte}' é backup/entulho de dados pessoais"

    nome_arquivo = alvo.name.casefold()
    for padrao in PRIVATE_FILE_PATTERNS:
        if fnmatch(nome_arquivo, padrao):
            return (
                f"arquivos no padrão '{padrao}' guardam dados ou credenciais "
                f"e nunca são expostos por este servidor"
            )

    return None


def extensao_legivel(alvo: Path) -> bool:
    """Segunda camada: só sai conteúdo de formato de texto/código conhecido."""
    return alvo.suffix.casefold() in READABLE_EXTENSIONS


# ============================================================
# Portões usados pelas tools
# ============================================================

def pode_indexar(alvo: Path) -> bool:
    """Entra no índice? Metadado (tamanho, data, hash) de arquivo privado não entra."""
    return not esta_ignorado(alvo) and motivo_privacidade(alvo) is None


def iterar_indexaveis(raiz: Path):
    """Percorre uma árvore devolvendo só os arquivos que a política indexa.

    A poda acontece na descida: uma pasta privada nem é aberta, então nenhum
    caminho de dentro dela chega a quem chamou — nem como metadado. É a única
    caminhada de disco do projeto (server, scanner e watcher usam esta).
    """
    for diretorio, subdiretorios, arquivos in os.walk(raiz, topdown=True):
        base = Path(diretorio)
        subdiretorios[:] = [
            nome for nome in subdiretorios
            if pode_indexar(base / nome)
        ]
        for nome in arquivos:
            caminho = base / nome
            if pode_indexar(caminho):
                yield caminho


def resolver_para_leitura(relative_path: str) -> Path:
    """Resolve um caminho e só devolve se o CONTEÚDO puder ser lido.

    Levanta `AcessoNegado` com o motivo em português. É o portão de
    `read_file`, `read_file_excerpt`, `get_file_metadata` e da busca.
    """
    alvo = resolver(relative_path)

    if esta_ignorado(alvo):
        raise AcessoNegado(
            f"Caminho dentro de pasta gerada/de ferramenta, fora do escopo: "
            f"{relative_path!r}."
        )

    motivo = motivo_privacidade(alvo)
    if motivo is not None:
        raise AcessoNegado(
            f"Acesso bloqueado a {relative_path!r}: {motivo}. "
            f"Este servidor expõe o código do projeto, não os dados da Gumi."
        )

    if alvo.is_file() and not extensao_legivel(alvo):
        raise AcessoNegado(
            f"Extensão '{alvo.suffix or '(sem extensão)'}' não é um formato de "
            f"texto liberado. Liberados: código, markdown, config. "
            f"Use get_stored_file_state para ver metadados sem ler o conteúdo."
        )

    return alvo


def descrever_bloqueio(alvo: Path) -> str | None:
    """Rótulo curto para listagens: mostra que existe, sem entregar o conteúdo."""
    if esta_ignorado(alvo):
        return "ignorado"
    if motivo_privacidade(alvo) is not None:
        return "privado"
    if alvo.is_file() and not extensao_legivel(alvo):
        return "binario"
    return None


def _partes(alvo: Path) -> tuple[str, ...]:
    """Partes do caminho relativas à raiz (a raiz em si nunca é analisada)."""
    try:
        return alvo.relative_to(GUMI_ROOT).parts
    except ValueError:
        return alvo.parts


# Nome antigo, mantido para não quebrar imports existentes.
secure_path = resolver
