"""L5 — Busca Real: FTS5 no gumi_state.db, mantido por watcher e scanner.

A busca devolve {path, linha, trecho} sem reler arquivo; com o índice frio
cai para a varredura literal; e a zona íntima nunca entra no índice de
conteúdo — nem por caminho, nem por trecho.
"""

import importlib
import json
import os
from types import SimpleNamespace

import pytest


@pytest.fixture
def servidor(workspace):
    return importlib.import_module("server")


@pytest.fixture
def varredura(workspace):
    return importlib.import_module("scanner")


@pytest.fixture
def handler(servidor, workspace):
    watcher = importlib.import_module("watcher")
    return watcher.GumiEventHandler(root=workspace, database=servidor.database)


def _dados(funcao, /, **kwargs):
    return json.loads(getattr(funcao, "fn", funcao)(**kwargs))


def _paths(resultado):
    return [item["path"] for item in resultado["resultados"]]


def _aquecer(servidor, varredura):
    return varredura.varrer(servidor.database)


# ============================================================
# O scanner alimenta o índice — e nada íntimo entra nele
# ============================================================

def test_scanner_aquece_o_indice_e_nada_intimo_entra(servidor, varredura):
    resultado = _aquecer(servidor, varredura)
    assert resultado["indexados_para_busca"] > 0

    paths = servidor.database.fts_paths()
    assert "app/servicos/conversa.py" in paths
    assert not any("data/" in path for path in paths)
    assert not any(path.startswith("exports") for path in paths)
    assert not any(path.endswith((".db", ".jsonl", ".env")) for path in paths)


def test_busca_fts_devolve_linha_e_trecho(servidor, varredura):
    _aquecer(servidor, varredura)
    resultado = _dados(servidor.search_project, query="marcador_de_busca")

    assert resultado["motor"] == "fts5"
    assert _paths(resultado) == ["app/servicos/conversa.py"]

    [item] = resultado["resultados"]
    assert item["linha"] == 2
    assert "marcador_de_busca" in item["trecho"]


def test_conteudo_intimo_nao_sai_nem_por_trecho(servidor, varredura):
    """O termo existe em app/data/perfil.json — e só lá."""
    _aquecer(servidor, varredura)
    resultado = _dados(servidor.search_project, query="marcador_privado")
    assert resultado["motor"] == "fts5"
    assert resultado["resultados"] == []


def test_parte_de_identificador_encontra_o_arquivo(servidor, varredura):
    """unicode61 quebra 'marcador_de_busca' em tokens: 'marcador' acha."""
    _aquecer(servidor, varredura)
    resultado = _dados(servidor.search_project, query="marcador")
    assert _paths(resultado) == ["app/servicos/conversa.py"]


def test_binario_disfarcado_de_texto_fica_fora_da_busca(
    servidor, varredura, workspace
):
    (workspace / "app" / "fake.py").write_bytes(b"import os\x00binario")
    _aquecer(servidor, varredura)

    assert "app/fake.py" not in servidor.database.fts_paths()
    # mas o metadado existe no índice de estado — indexar != buscar
    assert "app/fake.py" in servidor.database.get_active_paths()


# ============================================================
# O índice acompanha a vida dos arquivos
# ============================================================

def test_conteudo_alterado_e_apagado_atualizam_a_busca(
    servidor, varredura, workspace
):
    _aquecer(servidor, varredura)
    arquivo = workspace / "app" / "servicos" / "conversa.py"
    arquivo.write_text(
        "def nova():\n    return 'novo_marcador_unico'\n", encoding="utf-8"
    )
    os.utime(arquivo, (1_000_000_000, 1_000_000_000))
    _aquecer(servidor, varredura)

    assert _paths(_dados(servidor.search_project, query="novo_marcador_unico")) == [
        "app/servicos/conversa.py"
    ]
    assert _dados(servidor.search_project, query="marcador_de_busca")[
        "resultados"
    ] == []

    arquivo.unlink()
    _aquecer(servidor, varredura)
    assert _dados(servidor.search_project, query="novo_marcador_unico")[
        "resultados"
    ] == []


def test_watcher_alimenta_o_indice_de_busca(servidor, handler, workspace):
    novo = workspace / "app" / "novo_modulo.py"
    novo.write_text("VALOR = 'marcador_do_watcher'\n", encoding="utf-8")
    handler.on_created(SimpleNamespace(is_directory=False, src_path=str(novo)))

    resultado = _dados(servidor.search_project, query="marcador_do_watcher")
    assert resultado["motor"] == "fts5"
    assert _paths(resultado) == ["app/novo_modulo.py"]


def test_cascatas_de_diretorio_acompanham_na_busca(
    servidor, varredura, handler, workspace
):
    _aquecer(servidor, varredura)
    origem = workspace / "app" / "servicos"
    destino = workspace / "app" / "nucleo"
    origem.rename(destino)
    handler.on_moved(
        SimpleNamespace(
            is_directory=True, src_path=str(origem), dest_path=str(destino)
        )
    )

    resultado = _dados(servidor.search_project, query="marcador_de_busca")
    assert _paths(resultado) == ["app/nucleo/conversa.py"]

    handler.on_deleted(
        SimpleNamespace(is_directory=True, src_path=str(destino))
    )
    assert _dados(servidor.search_project, query="marcador_de_busca")[
        "resultados"
    ] == []


def test_purga_por_privacidade_limpa_a_busca(servidor, varredura):
    _aquecer(servidor, varredura)
    database = servidor.database
    assert "app/servicos/conversa.py" in database.fts_paths()

    database.delete_states(["app/servicos/conversa.py"])
    assert "app/servicos/conversa.py" not in database.fts_paths()


# ============================================================
# Paginação, escopo e planos B
# ============================================================

def test_paginacao_com_offset(servidor, varredura, workspace):
    for indice in range(3):
        (workspace / f"modulo_{indice}.py").write_text(
            f"# marcador_paginado arquivo {indice}\n", encoding="utf-8"
        )
    _aquecer(servidor, varredura)

    primeira = _dados(
        servidor.search_project, query="marcador_paginado", max_results=2
    )
    assert primeira["total"] == 3
    assert len(primeira["resultados"]) == 2
    assert primeira["next_offset"] == 2
    assert "como_continuar" in primeira

    segunda = _dados(
        servidor.search_project,
        query="marcador_paginado",
        max_results=2,
        offset=2,
    )
    assert len(segunda["resultados"]) == 1
    assert segunda["next_offset"] is None
    assert not set(_paths(primeira)) & set(_paths(segunda))


def test_escopo_restringe_a_busca(servidor, varredura, workspace):
    (workspace / "raiz_marcada.py").write_text(
        "# marcador_de_busca na raiz\n", encoding="utf-8"
    )
    _aquecer(servidor, varredura)

    resultado = _dados(
        servidor.search_project, query="marcador_de_busca", relative_path="app"
    )
    assert _paths(resultado) == ["app/servicos/conversa.py"]


def test_busca_recusa_escopo_intimo(servidor):
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError):
        _dados(servidor.search_project, query="x", relative_path="app/data")
    with pytest.raises(ToolError):
        _dados(servidor.search_project, query="x", relative_path=".venv")


def test_indice_frio_cai_para_varredura_literal(servidor):
    resultado = _dados(servidor.search_project, query="marcador_de_busca")

    assert resultado["motor"] == "varredura_literal"
    assert _paths(resultado) == ["app/servicos/conversa.py"]
    [item] = resultado["resultados"]
    assert item["linha"] == 2


def test_consulta_so_de_pontuacao_usa_a_varredura_literal(
    servidor, varredura, workspace
):
    (workspace / "simbolos.py").write_text("x = '???'\n", encoding="utf-8")
    _aquecer(servidor, varredura)

    resultado = _dados(servidor.search_project, query="???")
    assert resultado["motor"] == "varredura_literal"
    assert _paths(resultado) == ["simbolos.py"]
