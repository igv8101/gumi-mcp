"""A política de acesso — o que este servidor promete não deixar sair.

Cada teste aqui corresponde a uma falha reproduzida na análise de 02/09/2026.
"""

import importlib

import pytest


def _politica():
    return importlib.import_module("security")


# ============================================================
# Fronteira do workspace
# ============================================================

def test_caminho_relativo_comum_resolve(workspace):
    seguranca = _politica()
    alvo = seguranca.resolver("app/servicos/conversa.py")
    assert alvo.name == "conversa.py"
    assert alvo.is_file()


@pytest.mark.parametrize(
    "escape",
    [
        "../fora.txt",
        "app/../../fora.txt",
        "../../../../../../etc/passwd",
    ],
)
def test_escape_por_ponto_ponto_e_negado(workspace, escape):
    seguranca = _politica()
    with pytest.raises(seguranca.ForaDoWorkspace):
        seguranca.resolver(escape)


def test_caminho_absoluto_fora_e_negado(workspace, tmp_path):
    seguranca = _politica()
    with pytest.raises(seguranca.ForaDoWorkspace):
        seguranca.resolver(str(tmp_path / "fora.txt"))


# ============================================================
# F1 — zona privada de dados
# ============================================================

@pytest.mark.parametrize(
    "caminho",
    [
        "app/data/perfil.json",
        "app/data/registros/nota.txt",
        "exports/conversa.txt",
        ".env",
        "indice.db",
        "app/eventos.jsonl",
    ],
)
def test_dado_pessoal_nao_e_lido(workspace, caminho):
    seguranca = _politica()
    with pytest.raises(seguranca.AcessoNegado):
        seguranca.resolver_para_leitura(caminho)


def test_motivo_do_bloqueio_explica_o_porque(workspace):
    seguranca = _politica()
    with pytest.raises(seguranca.AcessoNegado) as erro:
        seguranca.resolver_para_leitura("app/data/perfil.json")
    mensagem = str(erro.value)
    assert "data" in mensagem
    assert "pessoais" in mensagem


def test_dado_pessoal_nao_entra_no_indice(workspace):
    seguranca = _politica()
    assert not seguranca.pode_indexar(workspace / "app" / "data" / "perfil.json")
    assert not seguranca.pode_indexar(workspace / "exports" / "conversa.txt")
    assert seguranca.pode_indexar(workspace / "app" / "servicos" / "conversa.py")


# ============================================================
# F2 — pastas ignoradas valem para a leitura também
# ============================================================

def test_pasta_ignorada_nao_e_lida(workspace):
    seguranca = _politica()
    with pytest.raises(seguranca.AcessoNegado):
        seguranca.resolver_para_leitura(".venv/lib.py")


def test_ignorado_e_insensivel_a_maiusculas(workspace):
    """F9: no Windows '.VENV' e '.venv' são a mesma pasta."""
    seguranca = _politica()
    assert seguranca.esta_ignorado(workspace / ".VENV" / "x.py")
    assert seguranca.esta_ignorado(workspace / "__PYCACHE__" / "x.pyc")


# ============================================================
# Segunda camada: extensão
# ============================================================

def test_binario_fora_da_zona_intima_tambem_nao_sai(workspace):
    seguranca = _politica()
    with pytest.raises(seguranca.AcessoNegado):
        seguranca.resolver_para_leitura("logo.png")


def test_codigo_e_markdown_passam(workspace):
    seguranca = _politica()
    assert seguranca.resolver_para_leitura("app/servicos/conversa.py").is_file()
    assert seguranca.resolver_para_leitura("README.md").is_file()


# ============================================================
# Listagem: mostra que existe, sem entregar
# ============================================================

def test_listagem_rotula_sem_esconder(workspace):
    seguranca = _politica()
    assert seguranca.descrever_bloqueio(workspace / "app" / "data") == "privado"
    assert seguranca.descrever_bloqueio(workspace / ".venv") == "ignorado"
    assert seguranca.descrever_bloqueio(workspace / "logo.png") == "binario"
    assert seguranca.descrever_bloqueio(workspace / "README.md") is None


def test_caminho_relativo_usa_barra_normal(workspace):
    seguranca = _politica()
    alvo = seguranca.resolver("app/servicos/conversa.py")
    assert seguranca.caminho_relativo(alvo) == "app/servicos/conversa.py"
