"""As tools do servidor: erro acionável, privacidade e paginação.

Chamamos as funções por baixo do decorador (`.fn`), o que testa a lógica sem
subir o transporte stdio.
"""

import importlib
import json

import pytest


@pytest.fixture
def servidor(workspace):
    return importlib.import_module("server")


def _chamar(tool, **kwargs):
    # No mcp 2.1.1 o decorador registra a tool e devolve a própria função.
    return getattr(tool, "fn", tool)(**kwargs)


def _dados(tool, **kwargs):
    return json.loads(_chamar(tool, **kwargs))


# ============================================================
# F5 — o erro precisa ensinar o que fazer
# ============================================================

def test_arquivo_inexistente_sugere_list_files(servidor):
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError) as erro:
        _chamar(servidor.read_file, relative_path="app/nao_existe.py")
    assert "list_files" in str(erro.value)


def test_escape_devolve_toolerror_e_nao_excecao_crua(servidor):
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError) as erro:
        _chamar(servidor.read_file, relative_path="../fora.txt")
    assert "workspace" in str(erro.value).casefold()


def test_ler_diretorio_orienta_a_tool_certa(servidor):
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError) as erro:
        _chamar(servidor.read_file, relative_path="app")
    assert "list_files" in str(erro.value)


def test_event_type_invalido_lista_os_validos(servidor):
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError) as erro:
        _chamar(servidor.get_recent_changes, limit=5, event_type="inventado")
    assert "created" in str(erro.value)


# ============================================================
# F1 / F2 — nenhuma tool serve dado pessoal
# ============================================================

@pytest.mark.parametrize(
    "caminho",
    [
        "app/data/perfil.json",
        "app/data/registros/nota.txt",
        "exports/conversa.txt",
        ".env",
        "indice.db",
        ".venv/lib.py",
    ],
)
def test_read_file_recusa_zona_intima(servidor, caminho):
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError):
        _chamar(servidor.read_file, relative_path=caminho)


@pytest.mark.parametrize(
    "caminho",
    ["app/data/perfil.json", ".env", "exports/conversa.txt"],
)
def test_excerpt_e_metadata_recusam_zona_intima(servidor, caminho):
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError):
        _chamar(servidor.read_file_excerpt, relative_path=caminho)
    with pytest.raises(ToolError):
        _chamar(servidor.get_file_metadata, relative_path=caminho)


def test_stored_state_recusa_zona_intima(servidor):
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError):
        _chamar(
            servidor.get_stored_file_state,
            relative_path="app/data/perfil.json",
        )


def test_busca_nao_encontra_dado_pessoal(servidor):
    """O marcador existe em código E em dados íntimos. Só o código pode voltar."""
    resultado = _dados(servidor.search_project, query="marcador_de_busca")
    encontrados = [item["path"] for item in resultado["resultados"]]

    assert "app/servicos/conversa.py" in encontrados
    assert not any("data/" in caminho for caminho in encontrados)
    assert not any("exports" in caminho for caminho in encontrados)


def test_refresh_nao_indexa_dado_pessoal(servidor):
    _chamar(servidor.refresh_project_state, relative_path=".", max_files=500)
    database = servidor.database

    for caminho in database.get_active_paths():
        assert "data/" not in caminho
        assert not caminho.startswith("exports")
        assert not caminho.endswith(".db")
        assert not caminho.endswith(".jsonl")
        assert ".env" not in caminho


# ============================================================
# Leitura legítima continua funcionando
# ============================================================

def test_read_file_le_codigo(servidor):
    conteudo = _chamar(servidor.read_file, relative_path="app/servicos/conversa.py")
    assert "def responder" in conteudo


def test_excerpt_numera_e_pagina(servidor):
    dados = _dados(
        servidor.read_file_excerpt,
        relative_path="app/servicos/conversa.py",
        start_line=1,
        max_lines=1,
    )
    assert dados["linhas"][0]["linha"] == 1
    assert dados["next_line"] == 2


def test_listagem_marca_bloqueados_sem_omitir(servidor):
    dados = _dados(servidor.list_files, relative_path=".")
    por_nome = {entrada["nome"]: entrada for entrada in dados["entradas"]}

    assert por_nome["exports"]["bloqueado"] == "privado"
    assert por_nome[".env"]["bloqueado"] == "privado"
    assert "bloqueado" not in por_nome["README.md"]
    assert ".venv" not in por_nome  # ignorado sai da listagem


def test_listagem_pagina_com_cursor(servidor):
    primeira = _dados(servidor.list_files, relative_path=".", limit=2)
    assert len(primeira["entradas"]) == 2
    assert primeira["next_cursor"] is not None

    segunda = _dados(
        servidor.list_files,
        relative_path=".",
        limit=50,
        start_after=primeira["next_cursor"],
    )
    nomes_primeira = {e["nome"] for e in primeira["entradas"]}
    nomes_segunda = {e["nome"] for e in segunda["entradas"]}
    assert not (nomes_primeira & nomes_segunda)


def test_overview_nao_conta_zona_intima(servidor):
    """Conta README.md, app/servicos/conversa.py e logo.png.

    O PNG entra de propósito: um sprite é parte do projeto e seus metadados são
    úteis. Indexar não é o mesmo que servir o conteúdo — o teste do binário em
    test_seguranca.py garante que a leitura dele continua barrada. Fora da conta
    ficam data/, Samples/, .env, .db e .jsonl.
    """
    dados = _dados(servidor.project_overview)
    assert dados["arquivos_visiveis"] == 3


# ============================================================
# F7 — hash respeita o teto de tamanho
# ============================================================

def test_hash_nao_roda_em_arquivo_gigante(servidor, workspace, monkeypatch):
    monkeypatch.setattr(servidor, "MAX_FILE_SIZE", 10)
    alvo = workspace / "app" / "servicos" / "conversa.py"
    assert servidor.file_sha256(alvo) is None


def test_hash_normal_funciona(servidor, workspace):
    alvo = workspace / "app" / "servicos" / "conversa.py"
    assert len(servidor.file_sha256(alvo)) == 64
