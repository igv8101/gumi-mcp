"""L6 — A Gumi Usando: resources, prompts e o fechamento do círculo.

O último lote transforma ferramenta em órgão: contexto que o cliente puxa
sem gastar chamada de tool (resources gumi://workspace/estado e /saude) e
roteiros prontos de uso (prompts). Tudo auditado como as tools (L3), tudo
com o envelope de proveniência (L4).
"""

import importlib
import json

import pytest


@pytest.fixture
def servidor(workspace):
    return importlib.import_module("server")


def _chamar(funcao, /, **kwargs):
    return getattr(funcao, "fn", funcao)(**kwargs)


def _dados(funcao, /, **kwargs):
    return json.loads(_chamar(funcao, **kwargs))


# ============================================================
# Resources — contexto sem gastar chamada de tool
# ============================================================

def test_resources_registrados_com_mime_json(servidor):
    recursos = {
        str(r.uri): r for r in servidor.mcp._resource_manager.list_resources()
    }
    assert set(recursos) == {
        "gumi://workspace/estado",
        "gumi://workspace/saude",
    }
    for recurso in recursos.values():
        assert recurso.mime_type == "application/json"


def test_resource_estado_traz_envelope_e_indice(servidor):
    dados = _dados(servidor.recurso_estado)
    assert dados["fonte"] == "workspace_observado"
    assert dados["servidor"] == "gumi-mcp"
    assert "workspace" in dados
    # As mesmas chaves do resumo persistente do índice.
    resumo = servidor.database.get_state_summary()
    for chave in resumo:
        assert chave in dados


def test_resource_estado_e_a_visao_do_get_project_state(servidor):
    """O resource é o mesmo conteúdo da tool — só muda o canal."""
    do_recurso = _dados(servidor.recurso_estado)
    da_tool = _dados(servidor.get_project_state)
    do_recurso.pop("em")
    da_tool.pop("em")
    assert do_recurso == da_tool


def test_resource_saude_fora_do_main_nao_finge_saude(servidor):
    dados = _dados(servidor.recurso_saude)
    assert dados["watcher"]["ativo"] is False
    assert "não iniciado" in dados["watcher"]["observacao"]
    assert dados["scanner"]["ativo"] is False


def test_resource_saude_traz_os_blocos_essenciais(servidor):
    dados = _dados(servidor.recurso_saude)
    for bloco in ("servidor", "versao", "watcher", "scanner", "registro",
                  "estado", "indice_busca"):
        assert bloco in dados, f"bloco {bloco!r} sumiu da saúde"
    assert dados["versao"] == servidor.SERVER_VERSION


def test_get_project_health_continua_com_os_mesmos_blocos(servidor):
    """A refatoração (_info_watcher/_info_scanner) não pode ter mudado a tool."""
    dados = _dados(servidor.get_project_health)
    assert dados["watcher"]["ativo"] is False
    assert dados["scanner"]["ativo"] is False
    assert "maiores_arquivos" in dados


def test_leitura_de_resource_entra_na_trilha_de_auditoria(servidor):
    import audit

    _chamar(servidor.recurso_estado)
    linhas = audit.ler_linhas_finais(audit.caminho_auditoria(), 50)
    ferramentas = [json.loads(linha).get("tool") for linha in linhas]
    assert "recurso_estado" in ferramentas


# ============================================================
# Prompts — roteiros prontos de uso
# ============================================================

def test_prompts_registrados(servidor):
    nomes = {p.name for p in servidor.mcp._prompt_manager.list_prompts()}
    assert nomes == {
        "perceber_o_corpo",
        "acompanhar_mudancas",
        "investigar_arquivo",
    }


def test_perceber_o_corpo_ensina_o_ritual_e_a_regra(servidor):
    texto = _chamar(servidor.prompt_perceber_o_corpo)
    assert "get_changes_since" in texto
    assert "gumi://workspace/estado" in texto
    assert "workspace_observado" in texto
    # Contexto observado nunca vira emoção sobre a pessoa.
    assert "emoção" in texto
    assert "SOMENTE LEITURA" in texto


def test_acompanhar_mudancas_cobre_fluxo_rompido(servidor):
    texto = _chamar(servidor.prompt_acompanhar_mudancas)
    assert "fresh_instance" in texto
    assert "next_cursor" in texto


def test_investigar_arquivo_carrega_o_caminho_pedido(servidor):
    texto = _chamar(
        servidor.prompt_investigar_arquivo,
        relative_path="app/servicos/conversa.py",
    )
    assert "app/servicos/conversa.py" in texto
    assert "read_file_excerpt" in texto
    assert "somente leitura" in texto


def test_versao_do_lote(servidor):
    assert servidor.SERVER_VERSION == "2.5.0"
