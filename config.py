"""Configuração do Gumi-MCP.

Nada aqui é fixo no código: caminhos e listas de bloqueio vêm de variáveis de
ambiente, com padrões conservadores. Isso serve a três coisas ao mesmo tempo —
a suíte roda contra um workspace de mentira sem tocar no real, outra pessoa
usa o servidor no projeto dela sem editar código, e o repositório não precisa
carregar a estrutura de pastas de ninguém.
"""

import json
import os
from pathlib import Path


SERVER_NAME = "gumi-mcp"
SERVER_VERSION = "2.5.0"

_BASE = Path(__file__).resolve().parent


def _caminho(variavel: str, padrao: str) -> Path:
    return Path(os.environ.get(variavel, padrao)).expanduser().resolve()


# A raiz a servir. DEFINA `GUMI_MCP_ROOT` — sem ela, o servidor serve apenas a
# própria pasta do projeto.
#
# O padrão é a própria pasta, e não a pasta-pai, por segurança: um padrão que
# sobe um nível serviria tudo o que estiver ao lado deste projeto, o que é
# exatamente o tipo de surpresa que este servidor existe para evitar. Melhor
# servir de menos e a pessoa configurar do que servir demais em silêncio.
GUMI_ROOT = _caminho("GUMI_MCP_ROOT", str(_BASE))

DATABASE_PATH = _caminho("GUMI_MCP_DATABASE", str(_BASE / "data" / "gumi_state.db"))
LOG_DIRECTORY = _caminho("GUMI_MCP_LOGS", str(_BASE / "logs"))


# ============================================================
# EXCLUSÕES ESTRUTURAIS
# Pastas geradas, pesadas ou de ferramenta: ninguém quer indexar isso.
# Comparação sempre em casefold (o Windows não distingue maiúsculas).
# ============================================================

IGNORE_DIRECTORIES = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    ".next",
    ".cache",
    ".turbo",
    "build",
    "dist",
    "coverage",
    "target",
    ".idea",
    ".vscode",
    ".gradle",
}


# ============================================================
# ZONA PRIVADA — a regra mais importante deste servidor
#
# Leitura via MCP SAI DA MÁQUINA. Um repositório de projeto pessoal costuma
# guardar, ao lado do código, coisas que não são código: dados de uso,
# exports, mídia, bancos locais. Nada dessas pastas é lido, buscado ou
# hasheado por tool nenhuma.
#
# Elas continuam APARECENDO nas listagens, marcadas como bloqueadas —
# esconder criaria um mapa mentiroso do workspace; o que não pode é o
# conteúdo sair.
#
# Os padrões abaixo são conservadores de propósito. Para acrescentar os nomes
# do seu projeto sem versioná-los, crie um `privado.local.json` ao lado deste
# arquivo (já está no .gitignore):
#
#     {
#       "diretorios": ["minha_pasta_de_dados"],
#       "prefixos": ["_rascunho"],
#       "arquivos": ["*.meu_formato"]
#     }
# ============================================================

PRIVATE_DIRECTORIES = {
    "data",
    "dados",
    "datasets",
    "samples",
    "exports",
    "backups",
    "backup",
    "logs",
    "media",
    "midia",
    "galeria",
    "uploads",
    "private",
    "privado",
    "secrets",
    "memoria",
    "conversas",
}

# Prefixos de pasta que também são zona privada (rascunho, backup datado).
PRIVATE_DIRECTORY_PREFIXES = (
    "_backup",
    "_entulho",
    "_tmp",
    "_old",
)

# Nomes de arquivo bloqueados (glob, casefold).
PRIVATE_FILE_PATTERNS = (
    ".env*",
    "*.db",
    "*.db-wal",
    "*.db-shm",
    "*.sqlite",
    "*.sqlite3",
    "*.jsonl",
    "*.bak",
    "*.key",
    "*.pem",
    "*.pfx",
    "*.p12",
    "id_rsa*",
    "*credenciais*",
    "*credentials*",
    "*secret*",
    "*token*",
    "*.session",
)


def _extensoes_locais() -> None:
    """Funde `privado.local.json`, se existir. Falha em silêncio de propósito:
    um arquivo local quebrado não pode derrubar o servidor — mas também não
    pode AFROUXAR a política, então só sabe acrescentar."""
    arquivo = _BASE / "privado.local.json"
    try:
        with open(arquivo, encoding="utf-8") as fonte:
            extra = json.load(fonte)
    except (OSError, json.JSONDecodeError, ValueError):
        return

    global PRIVATE_DIRECTORY_PREFIXES, PRIVATE_FILE_PATTERNS

    for nome in extra.get("diretorios", []) or []:
        if isinstance(nome, str):
            PRIVATE_DIRECTORIES.add(nome.casefold())

    prefixos = tuple(
        nome.casefold() for nome in (extra.get("prefixos", []) or [])
        if isinstance(nome, str)
    )
    PRIVATE_DIRECTORY_PREFIXES = PRIVATE_DIRECTORY_PREFIXES + prefixos

    arquivos = tuple(
        nome.casefold() for nome in (extra.get("arquivos", []) or [])
        if isinstance(nome, str)
    )
    PRIVATE_FILE_PATTERNS = PRIVATE_FILE_PATTERNS + arquivos


_extensoes_locais()


# Segunda camada (defesa em profundidade): mesmo fora das pastas acima, só sai
# conteúdo de arquivo cuja extensão esteja aqui. Um dado que apareça num lugar
# novo, num formato de dados, continua não vazando.
READABLE_EXTENSIONS = {
    ".py", ".pyi", ".md", ".txt", ".rst",
    ".json", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".conf",
    ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
    ".html", ".htm", ".css", ".scss",
    ".kt", ".kts", ".java", ".xml", ".gradle", ".pro",
    ".sql", ".sh", ".bat", ".ps1", ".cmd",
    ".gitignore", ".editorconfig", ".properties", ".lock",
    "",  # arquivos sem extensão (LICENSE, Makefile)
}


# ============================================================
# LIMITES
# ============================================================

MAX_FILE_SIZE = 10 * 1024 * 1024        # 10 MB — teto para indexar/hashear
SCAN_INTERVAL_HOURS = float(os.environ.get("GUMI_MCP_SCAN_HOURS", "6"))
MAX_TEXT_RESPONSE_SIZE = 200 * 1024     # 200 KB — teto de uma resposta de texto
DEFAULT_REFRESH_BATCH_SIZE = 200
MAX_REFRESH_BATCH_SIZE = 1_000
WATCHER_DEBOUNCE_SECONDS = 0.75
