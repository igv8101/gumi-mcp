"""Configuração da suíte.

Todo teste roda contra um workspace de mentira criado em tmp_path. As variáveis
de ambiente são definidas ANTES de qualquer import do projeto, senão config.py
já teria congelado os caminhos reais.
"""

import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

MODULOS = (
    "server",
    "scanner",
    "watcher",
    "fulltext",
    "audit",
    "hashing",
    "database",
    "security",
    "config",
)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Workspace falso com um exemplar de cada situação que a política trata."""
    raiz = tmp_path / "workspace"

    # Código legítimo
    (raiz / "app" / "servicos").mkdir(parents=True)
    (raiz / "app" / "servicos" / "conversa.py").write_text(
        "def responder():\n    return 'ola'  # marcador_de_busca\n",
        encoding="utf-8",
    )
    (raiz / "README.md").write_text("# Projeto\n", encoding="utf-8")

    # Zona íntima: pasta de dados
    (raiz / "app" / "data" / "registros").mkdir(parents=True)
    (raiz / "app" / "data" / "perfil.json").write_text(
        '{"nome": "pessoa", "marcador_de_busca": true}', encoding="utf-8"
    )
    (raiz / "app" / "data" / "registros" / "lembranca.txt").write_text(
        "conteudo intimo marcador_de_busca", encoding="utf-8"
    )

    # Zona íntima: exports
    (raiz / "exports").mkdir()
    (raiz / "exports" / "conversa.txt").write_text("mensagens", encoding="utf-8")

    # Arquivos bloqueados por padrão de nome
    (raiz / ".env").write_text("TOKEN=segredo", encoding="utf-8")
    (raiz / "indice.db").write_bytes(b"SQLite format 3\x00")
    (raiz / "app" / "eventos.jsonl").write_text('{"a":1}\n', encoding="utf-8")

    # Pasta estrutural ignorada
    (raiz / ".venv").mkdir()
    (raiz / ".venv" / "lib.py").write_text("interno", encoding="utf-8")

    # Binário fora da zona íntima (extensão não legível)
    (raiz / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)

    # Alvo de escape: existe FORA do workspace
    (tmp_path / "fora.txt").write_text("nao pode sair", encoding="utf-8")

    monkeypatch.setenv("GUMI_MCP_ROOT", str(raiz))
    monkeypatch.setenv("GUMI_MCP_DATABASE", str(tmp_path / "estado.db"))
    monkeypatch.setenv("GUMI_MCP_LOGS", str(tmp_path / "logs"))

    for modulo in MODULOS:
        sys.modules.pop(modulo, None)

    yield raiz

    for modulo in MODULOS:
        sys.modules.pop(modulo, None)
