"""L3 — Olhos e Registro (F8): log em arquivo, trilha de auditoria, saúde v2.

A regra que atravessa o lote inteiro: NADA vai para stdout — em stdio, stdout
é o canal JSON-RPC e um byte solto derruba a conexão. Todo teste que gera
registro confere também que stdout ficou limpo.
"""

import importlib
import json

import pytest


@pytest.fixture
def servidor(workspace):
    return importlib.import_module("server")


@pytest.fixture
def registro(workspace):
    return importlib.import_module("audit")


def _chamar(funcao, /, **kwargs):
    return getattr(funcao, "fn", funcao)(**kwargs)


def _dados(funcao, /, **kwargs):
    return json.loads(_chamar(funcao, **kwargs))


def _entradas_da_trilha(registro):
    texto = registro.caminho_auditoria().read_text(encoding="utf-8")
    return [json.loads(linha) for linha in texto.strip().splitlines()]


# ============================================================
# O log vai para arquivo — nunca para stdout
# ============================================================

def test_log_vai_para_arquivo_e_nao_para_stdout(registro, capsys):
    registro.info("marcador_info_do_teste")
    registro.aviso("marcador_aviso_do_teste")
    registro.erro("marcador_erro_do_teste")

    saida = capsys.readouterr()
    assert saida.out == ""

    conteudo = registro.caminho_log().read_text(encoding="utf-8")
    assert "marcador_info_do_teste" in conteudo
    assert "marcador_aviso_do_teste" in conteudo
    assert "marcador_erro_do_teste" in conteudo


def test_contadores_do_resumo(registro):
    assert registro.resumo()["erros_desde_inicio"] == 0
    registro.erro("um erro")
    registro.aviso("um aviso")
    resumo = registro.resumo()
    assert resumo["erros_desde_inicio"] == 1
    assert resumo["avisos_desde_inicio"] == 1
    assert resumo["log_existe"] is True


def test_importar_audit_nao_cria_pasta_nem_arquivo(registro):
    """Lição do F10: importar não tem efeito colateral. O log nasce no uso."""
    assert not registro.caminho_log().exists()
    assert not registro.caminho_auditoria().exists()


# ============================================================
# Trilha de auditoria: toda chamada de tool vira linha JSONL
# ============================================================

def test_chamada_com_sucesso_vira_linha_na_trilha(servidor, registro, capsys):
    _chamar(servidor.read_file, relative_path="README.md")

    assert capsys.readouterr().out == ""

    ultima = _entradas_da_trilha(registro)[-1]
    assert ultima["tool"] == "read_file"
    assert ultima["argumentos"]["relative_path"] == "README.md"
    assert ultima["sucesso"] is True
    assert ultima["duracao_ms"] >= 0
    assert "em" in ultima


def test_chamada_bloqueada_registra_falha_e_motivo(servidor, registro):
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError):
        _chamar(servidor.read_file, relative_path=".env")

    ultima = _entradas_da_trilha(registro)[-1]
    assert ultima["tool"] == "read_file"
    assert ultima["sucesso"] is False
    assert ultima["argumentos"]["relative_path"] == ".env"
    assert "erro" in ultima


def test_trilha_trunca_argumento_gigante(servidor, registro):
    consulta = "x" * 5_000
    _chamar(servidor.search_project, query=consulta)

    ultima = _entradas_da_trilha(registro)[-1]
    assert len(ultima["argumentos"]["query"]) < 400
    assert "truncado" in ultima["argumentos"]["query"]


def test_falha_de_registro_nao_derruba_a_tool(servidor, registro, monkeypatch):
    """Log é apoio, não carga: se a trilha quebrar, a resposta ainda sai."""
    def explode(*args, **kwargs):
        raise OSError("disco de log de mentira cheio")

    monkeypatch.setattr(registro, "_auditoria", explode)
    conteudo = _chamar(servidor.read_file, relative_path="README.md")
    assert "Projeto" in conteudo


# ============================================================
# As tools de leitura do registro
# ============================================================

def test_get_server_log_sem_nada_ainda_explica(servidor):
    dados = _dados(servidor.get_server_log)
    assert dados["linhas"] == []
    assert "observacao" in dados


def test_get_server_log_devolve_as_linhas(servidor, registro):
    registro.info("marcador_visivel_pela_tool")
    dados = _dados(servidor.get_server_log, max_lines=50)
    assert any("marcador_visivel_pela_tool" in linha for linha in dados["linhas"])


def test_get_audit_trail_lista_e_filtra(servidor):
    from mcp.server.mcpserver.exceptions import ToolError

    _chamar(servidor.get_project_state)
    with pytest.raises(ToolError):
        _chamar(servidor.read_file, relative_path=".env")

    tudo = _dados(servidor.get_audit_trail)
    tools_vistas = {entrada["tool"] for entrada in tudo["entradas"]}
    assert "get_project_state" in tools_vistas
    assert "read_file" in tools_vistas

    so_read_file = _dados(servidor.get_audit_trail, tool="read_file")
    assert so_read_file["entradas"]
    assert all(e["tool"] == "read_file" for e in so_read_file["entradas"])

    so_falhas = _dados(servidor.get_audit_trail, only_errors=True)
    assert so_falhas["entradas"]
    assert all(e["sucesso"] is False for e in so_falhas["entradas"])


def test_get_audit_trail_registra_a_si_mesma(servidor):
    _chamar(servidor.get_audit_trail)
    segunda = _dados(servidor.get_audit_trail)
    assert any(e["tool"] == "get_audit_trail" for e in segunda["entradas"])


def test_get_audit_trail_respeita_limit(servidor):
    for _ in range(5):
        _chamar(servidor.get_project_state)
    dados = _dados(servidor.get_audit_trail, limit=2)
    assert len(dados["entradas"]) == 2


# ============================================================
# Watcher e scanner não engolem mais erro em silêncio
# ============================================================

def test_watcher_loga_falha_de_atualizacao(servidor, registro, workspace, monkeypatch):
    watcher = importlib.import_module("watcher")

    def explode(caminho, limite=None):
        raise OSError("disco de mentira falhou")

    monkeypatch.setattr(watcher, "sha256_do_arquivo", explode)
    handler = watcher.GumiEventHandler(root=workspace, database=servidor.database)

    novo = workspace / "app" / "servicos" / "novo_arquivo.py"
    novo.write_text("x = 1\n", encoding="utf-8")
    handler._atualizar_estado(str(novo))

    conteudo = registro.caminho_log().read_text(encoding="utf-8")
    assert "novo_arquivo.py" in conteudo
    assert registro.resumo()["avisos_desde_inicio"] >= 1


def test_scanner_loga_falha_sem_morrer(servidor, registro, monkeypatch, capsys):
    scanner = importlib.import_module("scanner")

    def explode(database, raiz=None):
        raise RuntimeError("varredura de mentira quebrou")

    monkeypatch.setattr(scanner, "varrer", explode)
    periodico = scanner.ScannerPeriodico(servidor.database)
    periodico.executar_uma_vez()  # não pode levantar exceção

    assert capsys.readouterr().out == ""
    conteudo = registro.caminho_log().read_text(encoding="utf-8")
    assert "varredura de reconciliação falhou" in conteudo
    assert "varredura de mentira quebrou" in conteudo  # traceback no log
    assert registro.resumo()["erros_desde_inicio"] >= 1


def test_scanner_registra_sucesso_e_carimbo(servidor, registro):
    scanner = importlib.import_module("scanner")
    periodico = scanner.ScannerPeriodico(servidor.database)
    assert periodico.ultimo_scan_em is None

    periodico.executar_uma_vez()

    assert periodico.ultimo_scan_em is not None
    assert periodico.ultimo_resultado["arquivos_no_disco"] >= 1
    assert "varredura de reconciliação concluída" in registro.caminho_log().read_text(
        encoding="utf-8"
    )


# ============================================================
# Saúde v2
# ============================================================

def test_health_reporta_watcher_scanner_e_registro(servidor):
    dados = _dados(servidor.get_project_health)

    assert dados["watcher"]["ativo"] is False  # em teste, main() nunca rodou
    assert "observacao" in dados["watcher"]
    assert dados["scanner"]["ativo"] is False
    assert dados["registro"]["erros_desde_inicio"] == 0
    assert dados["versao"]
    assert "estado" in dados
