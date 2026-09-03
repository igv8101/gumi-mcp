"""L2 — Índice Fiel: o banco conta a verdade mesmo quando o watcher dorme.

Cobre F3 (move para zona invisível), F4 (cascata de diretório), a varredura
de reconciliação (scanner.py) e o hash unificado. Os eventos do watchdog são
simulados com SimpleNamespace — a lógica testada é a do handler, não a do
sistema operacional.
"""

import importlib
import json
from types import SimpleNamespace

import pytest


@pytest.fixture
def servidor(workspace):
    return importlib.import_module("server")


@pytest.fixture
def handler(servidor, workspace):
    watcher = importlib.import_module("watcher")
    return watcher.GumiEventHandler(root=workspace, database=servidor.database)


def _dados(tool, **kwargs):
    return json.loads(getattr(tool, "fn", tool)(**kwargs))


def _indexar_tudo(servidor):
    _dados(servidor.refresh_project_state, relative_path=".", max_files=500)


# ============================================================
# F4 — apagar diretório cascateia para os filhos
# ============================================================

def test_apagar_diretorio_remove_os_filhos_do_indice(servidor, handler, workspace):
    _indexar_tudo(servidor)
    database = servidor.database
    assert "app/servicos/conversa.py" in database.get_active_paths()

    # No Windows, rmdir de árvore chega como UM evento do diretório.
    handler.on_deleted(
        SimpleNamespace(
            is_directory=True,
            src_path=str(workspace / "app" / "servicos"),
        )
    )

    assert "app/servicos/conversa.py" not in database.get_active_paths()
    estado = database.get_file_state("app/servicos/conversa.py")
    assert estado is not None and estado["exists_now"] == 0

    historico = database.recent_changes(limit=100, event_type="deleted")
    assert any(
        evento["path"] == "app/servicos/conversa.py" for evento in historico
    )


def test_cascata_nao_pega_diretorio_de_nome_parecido(servidor, handler, workspace):
    """'app/serv_icos' tem '_', curinga do LIKE — sem escapar, a cascata
    apagaria 'app/servXicos' junto."""
    database = servidor.database
    database.update_file_state("app/serv_icos/a.py", 1, 1.0, "a" * 64, True)
    database.update_file_state("app/servXicos/b.py", 1, 1.0, "b" * 64, True)

    handler.on_deleted(
        SimpleNamespace(
            is_directory=True,
            src_path=str(workspace / "app" / "serv_icos"),
        )
    )

    ativos = database.get_active_paths()
    assert "app/serv_icos/a.py" not in ativos
    assert "app/servXicos/b.py" in ativos


# ============================================================
# F4 — mover diretório re-mapeia o prefixo, sem rehashear
# ============================================================

def test_mover_diretorio_remapeia_os_filhos(servidor, handler, workspace):
    _indexar_tudo(servidor)
    database = servidor.database
    hash_antes = database.get_file_state("app/servicos/conversa.py")["sha256"]

    origem = workspace / "app" / "servicos"
    destino = workspace / "app" / "nucleo"
    origem.rename(destino)

    handler.on_moved(
        SimpleNamespace(
            is_directory=True,
            src_path=str(origem),
            dest_path=str(destino),
        )
    )

    ativos = database.get_active_paths()
    assert "app/servicos/conversa.py" not in ativos
    assert "app/nucleo/conversa.py" in ativos
    assert database.get_file_state("app/nucleo/conversa.py")["sha256"] == hash_antes

    historico = database.recent_changes(limit=100, event_type="moved")
    assert any(
        evento["path"] == "app/nucleo/conversa.py"
        and evento["old_path"] == "app/servicos/conversa.py"
        for evento in historico
    )


# ============================================================
# F3 — mover para zona invisível é, para o índice, deixar de existir
# ============================================================

def test_mover_arquivo_para_pasta_ignorada_vira_removido(servidor, handler, workspace):
    _indexar_tudo(servidor)
    database = servidor.database

    handler.on_moved(
        SimpleNamespace(
            is_directory=False,
            src_path=str(workspace / "app" / "servicos" / "conversa.py"),
            dest_path=str(workspace / ".venv" / "conversa.py"),
        )
    )

    ativos = database.get_active_paths()
    assert "app/servicos/conversa.py" not in ativos
    assert not any(".venv" in caminho for caminho in ativos)


def test_mover_diretorio_para_zona_ignorada_cascateia(servidor, handler, workspace):
    _indexar_tudo(servidor)
    database = servidor.database

    handler.on_moved(
        SimpleNamespace(
            is_directory=True,
            src_path=str(workspace / "app" / "servicos"),
            dest_path=str(workspace / ".venv" / "servicos"),
        )
    )

    ativos = database.get_active_paths()
    assert "app/servicos/conversa.py" not in ativos
    assert not any(".venv" in caminho for caminho in ativos)


def test_mover_diretorio_de_zona_ignorada_para_visivel_indexa(servidor, handler, workspace):
    """O caminho inverso do F3: a árvore aparece do nada e vira novidade."""
    database = servidor.database

    destino = workspace / "app" / "resgatado"
    destino.mkdir(parents=True)
    (destino / "modulo.py").write_text("x = 1\n", encoding="utf-8")

    handler.on_moved(
        SimpleNamespace(
            is_directory=True,
            src_path=str(workspace / ".venv" / "resgatado"),
            dest_path=str(destino),
        )
    )

    assert "app/resgatado/modulo.py" in database.get_active_paths()
    historico = database.recent_changes(limit=100, event_type="created")
    assert any(
        evento["path"] == "app/resgatado/modulo.py" for evento in historico
    )


# ============================================================
# scanner.py — a varredura conserta o que o watcher perdeu
# ============================================================

def test_varredura_detecta_mudancas_com_o_watcher_desligado(servidor, workspace):
    scanner = importlib.import_module("scanner")
    database = servidor.database
    _indexar_tudo(servidor)

    # Tudo o que acontece a seguir é "offline": nenhum watcher rodando.
    alterado = workspace / "app" / "servicos" / "conversa.py"
    alterado.write_text(
        "def responder():\n    return 'mudou bastante o conteudo'\n",
        encoding="utf-8",
    )
    novo = workspace / "app" / "servicos" / "surgiu.py"
    novo.write_text("y = 2\n", encoding="utf-8")
    (workspace / "README.md").unlink()

    resultado = scanner.varrer(database, workspace)

    assert resultado["novos"] >= 1
    assert resultado["modificados"] >= 1
    assert resultado["removidos"] >= 1

    ativos = database.get_active_paths()
    assert "app/servicos/surgiu.py" in ativos
    assert "README.md" not in ativos
    assert database.get_file_state("README.md")["exists_now"] == 0

    # O conteúdo alterado foi rehasheado e virou evento 'modified'.
    historico = database.recent_changes(limit=200)
    tipos_por_caminho = {
        (evento["event_type"], evento["path"]) for evento in historico
    }
    assert ("created", "app/servicos/surgiu.py") in tipos_por_caminho
    assert ("modified", "app/servicos/conversa.py") in tipos_por_caminho
    assert ("deleted", "README.md") in tipos_por_caminho


def test_varredura_purga_caminho_privado_do_indice(servidor, workspace):
    scanner = importlib.import_module("scanner")
    database = servidor.database

    # Índice legado: linha de dado pessoal gravada antes da política.
    database.update_file_state(
        "app/data/perfil.json", 10, 1.0, "a" * 64, True
    )

    resultado = scanner.varrer(database, workspace)

    assert resultado["apagados_por_privacidade"] >= 1
    assert "app/data/perfil.json" not in database.get_all_paths()


def test_varredura_nao_indexa_zona_intima(servidor, workspace):
    scanner = importlib.import_module("scanner")
    database = servidor.database

    scanner.varrer(database, workspace)

    for caminho in database.get_active_paths():
        assert "data/" not in caminho
        assert not caminho.startswith("exports")
        assert not caminho.endswith(".db")
        assert ".env" not in caminho


def test_varredura_em_workspace_sem_mudancas_nao_inventa_evento(servidor, workspace):
    scanner = importlib.import_module("scanner")
    database = servidor.database

    primeira = scanner.varrer(database, workspace)
    eventos_apos_primeira = len(database.recent_changes(limit=500))

    segunda = scanner.varrer(database, workspace)

    assert segunda["novos"] == 0
    assert segunda["modificados"] == 0
    assert segunda["removidos"] == 0
    assert len(database.recent_changes(limit=500)) == eventos_apos_primeira
    assert primeira["arquivos_no_disco"] == segunda["arquivos_no_disco"]


# ============================================================
# Hash unificado + rowcount honesto
# ============================================================

def test_hash_e_um_so_para_o_projeto_inteiro(servidor, workspace):
    hashing = importlib.import_module("hashing")
    watcher = importlib.import_module("watcher")

    alvo = workspace / "app" / "servicos" / "conversa.py"
    assert watcher.calculate_sha256 is hashing.sha256_do_arquivo
    assert servidor.file_sha256(alvo) == hashing.sha256_do_arquivo(alvo)
    assert hashing.sha256_do_arquivo(alvo, limite=1) is None
    assert hashing.sha256_do_arquivo(workspace / "nao_existe.py") is None


def test_marcar_removido_devolve_rowcount_real(servidor):
    database = servidor.database
    database.update_file_state("app/um.py", 1, 1.0, "a" * 64, True)

    assert database.mark_files_deleted(["app/um.py", "app/fantasma.py"]) == 1
    # Segunda passada: nada ativo para marcar — antes mentia '2'.
    assert database.mark_files_deleted(["app/um.py", "app/fantasma.py"]) == 0
