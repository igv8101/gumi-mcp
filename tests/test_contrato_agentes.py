"""L4 — Contrato para Agentes: cursor sem perda, integridade e proveniência.

O consumidor final é a Gumi: polling incremental que não perde nem repete
evento (get_changes_since, modelo Watchman), verificação do disco contra a
baseline (verify_integrity, modelo AIDE/Tripwire), envelope de proveniência
em toda resposta JSON e annotations de somente-leitura em toda tool.
"""

import importlib
import json
import os

import pytest


@pytest.fixture
def servidor(workspace):
    return importlib.import_module("server")


def _chamar(funcao, /, **kwargs):
    return getattr(funcao, "fn", funcao)(**kwargs)


def _dados(funcao, /, **kwargs):
    return json.loads(_chamar(funcao, **kwargs))


# ============================================================
# get_changes_since — polling sem perder nem repetir evento
# ============================================================

def test_primeira_chamada_e_fresh_e_da_cursor(servidor):
    dados = _dados(servidor.get_changes_since)
    assert dados["fresh_instance"] is True
    assert dados["changes"] == []
    assert ":" in dados["next_cursor"]
    assert "como_continuar" in dados


def test_eventos_chegam_em_ordem_e_sem_repeticao(servidor):
    cursor = _dados(servidor.get_changes_since)["next_cursor"]

    servidor.database.record_change("created", "app/a.py")
    servidor.database.record_change("modified", "app/b.py")

    dados = _dados(servidor.get_changes_since, cursor=cursor)
    assert dados["fresh_instance"] is False
    assert [c["path"] for c in dados["changes"]] == ["app/a.py", "app/b.py"]
    assert "timestamp" in dados["changes"][0]  # detailed é o padrão

    de_novo = _dados(servidor.get_changes_since, cursor=dados["next_cursor"])
    assert de_novo["changes"] == []
    assert de_novo["fresh_instance"] is False


def test_paginacao_nao_perde_evento_no_meio(servidor):
    """A falha do get_recent_changes: eventos entre consultas se perdiam."""
    cursor = _dados(servidor.get_changes_since)["next_cursor"]
    for indice in range(5):
        servidor.database.record_change("created", f"app/arq_{indice}.py")

    vistos = []
    for _ in range(3):
        pagina = _dados(servidor.get_changes_since, cursor=cursor, limit=2)
        vistos += [c["path"] for c in pagina["changes"]]
        cursor = pagina["next_cursor"]

    assert vistos == [f"app/arq_{i}.py" for i in range(5)]


def test_cursor_invalido_ensina_o_formato(servidor):
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError) as erro:
        _chamar(servidor.get_changes_since, cursor="rabisco")
    assert "sem cursor" in str(erro.value)


def test_cursor_de_outra_instancia_vira_fresh(servidor):
    dados = _dados(servidor.get_changes_since, cursor="outra-instancia:1")
    assert dados["fresh_instance"] is True
    assert dados["changes"] == []


def test_historico_compactado_vira_fresh(servidor):
    """Buraco no fluxo nunca vira resposta parcial — vira fresh_instance."""
    cursor = _dados(servidor.get_changes_since)["next_cursor"]
    for indice in range(4):
        servidor.database.record_change("created", f"app/c_{indice}.py")
    _chamar(servidor.optimize_state_database, retain_changes=1)

    dados = _dados(servidor.get_changes_since, cursor=cursor)
    assert dados["fresh_instance"] is True
    assert dados["changes"] == []


def test_response_format_concise_enxuga_os_eventos(servidor):
    cursor = _dados(servidor.get_changes_since)["next_cursor"]
    servidor.database.record_change("created", "app/x.py")

    dados = _dados(
        servidor.get_changes_since, cursor=cursor, response_format="concise"
    )
    assert set(dados["changes"][0]) == {"id", "event_type", "path"}


def test_response_format_invalido_ensina(servidor):
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError) as erro:
        _chamar(servidor.get_changes_since, response_format="resumido")
    assert "concise" in str(erro.value)


# ============================================================
# verify_integrity — disco × baseline (AIDE/Tripwire)
# ============================================================

@pytest.fixture
def baseline(servidor):
    """Servidor com o workspace de mentira já indexado."""
    _chamar(servidor.refresh_project_state, relative_path=".", max_files=500)
    return servidor


def test_baseline_intacta_da_tudo_igual(baseline):
    dados = _dados(baseline.verify_integrity)
    assert dados["contagem"]["modificados"] == 0
    assert dados["contagem"]["sumidos"] == 0
    assert dados["contagem"]["novos"] == 0
    assert dados["contagem"]["iguais"] >= 3


def test_detecta_modificado_sumido_e_novo(baseline, workspace):
    (workspace / "README.md").write_text("# Projeto v2\n", encoding="utf-8")
    (workspace / "app" / "servicos" / "conversa.py").unlink()
    (workspace / "novo_modulo.py").write_text("x = 1\n", encoding="utf-8")

    dados = _dados(baseline.verify_integrity)
    assert [m["path"] for m in dados["modificados"]] == ["README.md"]
    assert dados["sumidos"] == ["app/servicos/conversa.py"]
    assert dados["novos"] == ["novo_modulo.py"]


def test_check_hash_pega_conteudo_com_metadados_iguais(baseline, workspace):
    """Alteração que preserva tamanho e mtime só cai com check_hash=true."""
    alvo = workspace / "README.md"
    antes = alvo.stat()
    alvo.write_text("# Pxojeto\n", encoding="utf-8")  # mesmo tamanho
    os.utime(alvo, (antes.st_atime, antes.st_mtime))  # mesma data

    sem_hash = _dados(baseline.verify_integrity)
    assert sem_hash["contagem"]["modificados"] == 0  # metadado não enxerga

    com_hash = _dados(baseline.verify_integrity, check_hash=True)
    assert {"path": "README.md", "motivo": "conteudo"} in com_hash["modificados"]


def test_verify_num_arquivo_so(baseline):
    dados = _dados(baseline.verify_integrity, relative_path="README.md")
    assert dados["contagem"]["iguais"] == 1
    assert dados["escopo"] == "README.md"


def test_verify_recusa_zona_intima(baseline):
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError):
        _chamar(baseline.verify_integrity, relative_path="app/data")


def test_verify_concise_da_so_as_contagens(baseline):
    dados = _dados(baseline.verify_integrity, response_format="concise")
    assert "contagem" in dados
    assert "modificados" not in dados  # a lista fica de fora
    assert "novos" not in dados


def test_verify_lista_cortada_ensina_a_continuar(baseline, workspace):
    for indice in range(4):
        (workspace / f"extra_{indice}.py").write_text("y = 2\n", encoding="utf-8")

    dados = _dados(baseline.verify_integrity, max_listed=2)
    assert dados["contagem"]["novos"] == 4
    assert len(dados["novos"]) == 2
    assert "como_continuar" in dados


# ============================================================
# Envelope de proveniência e annotations (o contrato em si)
# ============================================================

def test_toda_resposta_json_carrega_proveniencia(servidor):
    respostas = (
        _dados(servidor.get_project_state),
        _dados(servidor.list_files, relative_path="."),
        _dados(servidor.get_changes_since),
        _dados(servidor.verify_integrity),
        _dados(servidor.get_project_health),
    )
    for dados in respostas:
        assert dados["fonte"] == "workspace_observado"
        assert dados["servidor"] == "gumi-mcp"
        assert dados["versao"]
        assert "em" in dados


def test_toda_tool_declara_somente_leitura(servidor):
    tools = servidor.mcp._tool_manager.list_tools()
    assert len(tools) == 17  # 15 do L1-L3 + get_changes_since + verify_integrity
    for tool in tools:
        assert tool.annotations is not None, tool.name
        assert tool.annotations.read_only_hint is True, tool.name
        assert tool.annotations.open_world_hint is False, tool.name
        assert tool.annotations.idempotent_hint is True, tool.name
