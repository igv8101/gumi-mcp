"""O índice não pode guardar nem o NOME de um arquivo da zona íntima."""

import importlib
import json

import pytest


@pytest.fixture
def servidor(workspace):
    return importlib.import_module("server")


def _dados(tool, **kwargs):
    return json.loads(getattr(tool, "fn", tool)(**kwargs))


def test_reconcile_apaga_caminho_privado_do_indice(servidor):
    """Simula o índice legado: construído antes da política existir."""
    database = servidor.database

    database.update_file_state(
        path="app/data/perfil.json",
        size=10,
        modified_at=1.0,
        sha256="a" * 64,
        exists_now=True,
    )
    database.update_file_state(
        path="exports/conversa.txt",
        size=20,
        modified_at=2.0,
        sha256="b" * 64,
        exists_now=True,
    )
    database.record_change(
        event_type="modified",
        path="app/data/perfil.json",
    )

    assert "app/data/perfil.json" in database.get_all_paths()

    resultado = _dados(servidor.reconcile_project_state)
    assert resultado["apagados_por_privacidade"] >= 2

    conhecidos = database.get_all_paths()
    assert "app/data/perfil.json" not in conhecidos
    assert "exports/conversa.txt" not in conhecidos

    # E o histórico de alterações também não guarda o caminho.
    historico = json.dumps(database.recent_changes(limit=100))
    assert "perfil" not in historico


def test_reconcile_preserva_historico_de_arquivo_que_so_sumiu(servidor, workspace):
    """Arquivo legítimo apagado do disco vira 'removido', não é esquecido."""
    database = servidor.database

    alvo = workspace / "app" / "servicos" / "temporario.py"
    alvo.write_text("x = 1\n", encoding="utf-8")
    _dados(servidor.refresh_project_state, relative_path=".", max_files=500)
    assert "app/servicos/temporario.py" in database.get_active_paths()

    alvo.unlink()
    _dados(servidor.reconcile_project_state)

    estado = database.get_file_state("app/servicos/temporario.py")
    assert estado is not None
    assert estado["exists_now"] == 0


def test_refresh_indexa_o_codigo(servidor):
    resultado = _dados(servidor.refresh_project_state, relative_path=".", max_files=500)
    assert resultado["atualizados"] >= 2
    ativos = servidor.database.get_active_paths()
    assert "app/servicos/conversa.py" in ativos
    assert "README.md" in ativos
