"""Gumi-MCP — servidor MCP somente leitura do workspace da Gumi.

Regras que valem para o arquivo inteiro:

* Nada aqui escreve, move ou executa qualquer coisa dentro do workspace.
* Nenhum `print()`: em stdio o stdout é o canal JSON-RPC e qualquer byte solto
  derruba a conexão.
* Todo caminho passa pela política de `security.py` antes de virar acesso.
* Todo erro previsível vira `ToolError` com mensagem em português dizendo o que
  fazer a seguir — exceção crua chega ao modelo como "Error executing tool X".
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from config import (
    GUMI_ROOT,
    DATABASE_PATH,
    IGNORE_DIRECTORIES,
    MAX_FILE_SIZE,
    MAX_TEXT_RESPONSE_SIZE,
    DEFAULT_REFRESH_BATCH_SIZE,
    MAX_REFRESH_BATCH_SIZE,
    SERVER_NAME,
    SERVER_VERSION,
)

import audit
from database import GumiDatabase
from fulltext import (
    frase_para_match,
    localizar_trecho,
    termos_da_consulta,
    texto_para_indexar,
)
from hashing import sha256_do_arquivo
from scanner import ScannerPeriodico
from security import (
    AcessoNegado,
    caminho_relativo,
    descrever_bloqueio,
    esta_ignorado,
    extensao_legivel,
    iterar_indexaveis,
    motivo_privacidade,
    pode_indexar,
    resolver,
    resolver_para_leitura,
)
from watcher import GumiWatcher


database = GumiDatabase(DATABASE_PATH)

# Preenchidos por main(). Fora do main() (testes, import) ficam None e o
# get_project_health diz isso com todas as letras em vez de fingir saúde.
_watcher: GumiWatcher | None = None
_scanner: ScannerPeriodico | None = None


mcp = MCPServer(
    SERVER_NAME,
    version=SERVER_VERSION,
    instructions=f"""
    Workspace local da Gumi, SOMENTE LEITURA: {GUMI_ROOT}

    Comece sempre pelo estado persistente, não por varredura:
    project_overview, get_project_state, get_recent_changes,
    get_stored_file_state. Um watcher mantém esse estado em SQLite.

    Para acompanhar mudanças sem perder nem repetir eventos, use
    get_changes_since e guarde sempre o next_cursor devolvido. Para
    conferir o disco contra a baseline do índice, use verify_integrity.
    Para procurar texto, use search_project: a resposta já traz caminho,
    linha e trecho, vindos do índice de busca — sem reler arquivos.

    Os dados pessoais (pastas de dados, exports, mídia, backups, e
    arquivos .db/.jsonl/.env) são BLOQUEADOS de propósito. Eles aparecem nas
    listagens marcados como privados, mas o conteúdo nunca é servido. Não
    tente contornar: não é falha, é o desenho.

    Contexto pronto sem gastar chamada de tool: os resources
    gumi://workspace/estado (índice em números) e gumi://workspace/saude
    (watcher, scanner, registro). Roteiros de uso: os prompts
    perceber_o_corpo, acompanhar_mudancas e investigar_arquivo.

    Nada aqui modifica, cria, apaga, move ou executa arquivos.
    """,
)


# Contrato L4: toda tool é somente leitura, alcança só o workspace local e
# pode ser repetida sem efeito novo. Vale para TODAS — inclusive as que
# escrevem no banco interno data/gumi_state.db, que é cache do servidor,
# não ambiente: o workspace da Gumi nunca muda por aqui.
ANOTACOES_LEITURA = ToolAnnotations(
    read_only_hint=True,
    open_world_hint=False,
    idempotent_hint=True,
)


# ============================================================
# HELPERS
# ============================================================

def _erro_de_caminho(exc: Exception, relative_path: str) -> ToolError:
    """Traduz exceções de caminho em mensagem que o modelo consegue agir."""
    if isinstance(exc, AcessoNegado):
        return ToolError(str(exc))
    if isinstance(exc, FileNotFoundError):
        return ToolError(
            f"Caminho não encontrado: {relative_path!r}. "
            f"Confira com list_files no diretório pai."
        )
    return ToolError(f"Caminho inválido {relative_path!r}: {exc}")


def file_sha256(path: Path) -> str | None:
    """SHA-256 pelo módulo unificado (L2). None acima do teto — hash de
    arquivo gigante é custo sem retorno e trava a resposta (F7)."""
    return sha256_do_arquivo(path, limite=MAX_FILE_SIZE)


def iter_workspace_files(root: Path):
    """Percorre o workspace já respeitando exclusões e privacidade.

    Delegado à política (`security.iterar_indexaveis`) — a mesma caminhada
    que o scanner e o watcher usam (L2).
    """
    yield from iterar_indexaveis(root)


def _json(payload) -> str:
    """Resposta JSON com envelope de proveniência (L4).

    Tudo que sai daqui é contexto observado do workspace — quem consome
    (a Gumi inclusive) rotula como observação e nunca deriva emoção disso
    (princípio 8). Em colisão de chave, o payload vence.
    """
    envelope = {
        "fonte": "workspace_observado",
        "servidor": SERVER_NAME,
        "versao": SERVER_VERSION,
        "em": datetime.now(timezone.utc).isoformat(),
    }
    return json.dumps({**envelope, **payload}, indent=2, ensure_ascii=False)


def _info_watcher() -> dict:
    """Bloco `watcher` da saúde — compartilhado por tool e resource (L6)."""
    if _watcher is None:
        return {
            "ativo": False,
            "observacao": "watcher não iniciado nesta execução (só sobe via main).",
        }
    return {"ativo": bool(_watcher.observer.is_alive())}


def _info_scanner() -> dict:
    """Bloco `scanner` da saúde — compartilhado por tool e resource (L6)."""
    if _scanner is None:
        return {
            "ativo": False,
            "observacao": "scanner não iniciado nesta execução (só sobe via main).",
        }
    return {
        "ativo": _scanner.esta_vivo(),
        "intervalo_horas": _scanner.intervalo_horas,
        "ultimo_scan_em": _scanner.ultimo_scan_em,
        "ultimo_resultado": _scanner.ultimo_resultado,
    }


# ============================================================
# TOOL: PROJECT OVERVIEW
# ============================================================

@mcp.tool(annotations=ANOTACOES_LEITURA)
@audit.auditar
def project_overview() -> str:
    """Conta arquivos e diretórios visíveis do workspace, ao vivo.

    Varre o disco: para a visão barata (vinda do índice), use get_project_state.
    """
    if not GUMI_ROOT.exists():
        raise ToolError(
            f"Workspace não encontrado em {GUMI_ROOT}. "
            f"Ajuste GUMI_ROOT no config.py ou a variável GUMI_MCP_ROOT."
        )

    arquivos = 0
    diretorios = 0

    for diretorio, subdiretorios, nomes in os.walk(GUMI_ROOT, topdown=True):
        base = Path(diretorio)
        subdiretorios[:] = [
            nome for nome in subdiretorios if pode_indexar(base / nome)
        ]
        diretorios += len(subdiretorios)
        arquivos += sum(1 for nome in nomes if pode_indexar(base / nome))

    return _json(
        {
            "workspace": str(GUMI_ROOT),
            "arquivos_visiveis": arquivos,
            "diretorios_visiveis": diretorios,
            "observacao": (
                "Pastas de dados pessoais e geradas ficam fora desta contagem."
            ),
        }
    )


# ============================================================
# TOOL: PROJECT STATE
# ============================================================

@mcp.tool(annotations=ANOTACOES_LEITURA)
@audit.auditar
def get_project_state() -> str:
    """Resumo do estado persistente conhecido (índice), sem tocar no disco."""
    return _json({"workspace": str(GUMI_ROOT), **database.get_state_summary()})


# ============================================================
# TOOL: LIST FILES
# ============================================================

@mcp.tool(annotations=ANOTACOES_LEITURA)
@audit.auditar
def list_files(
    relative_path: str = ".",
    limit: int = 200,
    start_after: str | None = None,
) -> str:
    """Lista um diretório, com tamanho e marcação de conteúdo bloqueado.

    Paginado: reenvie o `next_cursor` recebido em `start_after` para continuar.
    Entradas com `bloqueado` existem no disco mas não têm o conteúdo servido —
    "privado" é zona de dados pessoais, "ignorado" é pasta gerada.
    """
    try:
        alvo = resolver(relative_path)
    except Exception as exc:
        raise _erro_de_caminho(exc, relative_path) from exc

    if not alvo.exists():
        raise ToolError(
            f"Diretório não encontrado: {relative_path!r}. "
            f"Comece por list_files com relative_path='.'"
        )

    if not alvo.is_dir():
        raise ToolError(
            f"{relative_path!r} é um arquivo, não um diretório. "
            f"Use read_file para ler o conteúdo."
        )

    limit = max(1, min(limit, 1_000))

    try:
        itens = sorted(alvo.iterdir(), key=lambda item: item.name.casefold())
    except OSError as exc:
        raise ToolError(f"Não consegui listar {relative_path!r}: {exc}") from exc

    itens = [item for item in itens if not esta_ignorado(item)]

    if start_after:
        corte = start_after.casefold()
        itens = [item for item in itens if item.name.casefold() > corte]

    pagina = itens[:limit]
    entradas = []

    for item in pagina:
        entrada = {
            "nome": item.name,
            "tipo": "diretorio" if item.is_dir() else "arquivo",
        }

        bloqueio = descrever_bloqueio(item)
        if bloqueio:
            entrada["bloqueado"] = bloqueio
        elif item.is_file():
            try:
                entrada["bytes"] = item.stat().st_size
            except OSError:
                pass

        entradas.append(entrada)

    restantes = len(itens) - len(pagina)

    return _json(
        {
            "caminho": caminho_relativo(alvo) if alvo != GUMI_ROOT else ".",
            "entradas": entradas,
            "restantes": max(0, restantes),
            "next_cursor": pagina[-1].name if restantes > 0 and pagina else None,
        }
    )


# ============================================================
# TOOL: READ FILE
# ============================================================

@mcp.tool(annotations=ANOTACOES_LEITURA)
@audit.auditar
def read_file(relative_path: str) -> str:
    """Lê um arquivo de texto ou código inteiro.

    Recusa dados pessoais, pastas geradas e formatos que não sejam texto.
    Para arquivo grande, use read_file_excerpt.
    """
    try:
        alvo = resolver_para_leitura(relative_path)
    except Exception as exc:
        raise _erro_de_caminho(exc, relative_path) from exc

    if not alvo.exists():
        raise ToolError(
            f"Arquivo não encontrado: {relative_path!r}. "
            f"Confira o diretório com list_files."
        )

    if not alvo.is_file():
        raise ToolError(
            f"{relative_path!r} é um diretório. Use list_files para ver o conteúdo."
        )

    tamanho = alvo.stat().st_size
    if tamanho > MAX_FILE_SIZE:
        raise ToolError(
            f"Arquivo de {tamanho} bytes passa do limite de {MAX_FILE_SIZE}. "
            f"Use read_file_excerpt para ler por trechos."
        )

    try:
        conteudo = alvo.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ToolError(f"Não consegui ler {relative_path!r}: {exc}") from exc

    if len(conteudo.encode("utf-8")) > MAX_TEXT_RESPONSE_SIZE:
        raise ToolError(
            f"Conteúdo passa de {MAX_TEXT_RESPONSE_SIZE} bytes para uma resposta "
            f"única. Use read_file_excerpt(relative_path={relative_path!r}, "
            f"start_line=1) e siga pelo next_line."
        )

    return conteudo


@mcp.tool(annotations=ANOTACOES_LEITURA)
@audit.auditar
def read_file_excerpt(
    relative_path: str,
    start_line: int = 1,
    max_lines: int = 300,
) -> str:
    """Lê um trecho numerado de um arquivo, sem estourar a resposta.

    Devolve `next_line` quando ainda há conteúdo — reenvie em `start_line`.
    """
    try:
        alvo = resolver_para_leitura(relative_path)
    except Exception as exc:
        raise _erro_de_caminho(exc, relative_path) from exc

    if not alvo.exists() or not alvo.is_file():
        raise ToolError(
            f"Arquivo não encontrado: {relative_path!r}. "
            f"Confira o diretório com list_files."
        )

    tamanho = alvo.stat().st_size
    if tamanho > MAX_FILE_SIZE:
        raise ToolError(
            f"Arquivo de {tamanho} bytes passa do limite de {MAX_FILE_SIZE} "
            f"e não é lido nem por trechos."
        )

    start_line = max(1, start_line)
    max_lines = max(1, min(max_lines, 2_000))

    linhas = []
    proxima = None
    acumulado = 0

    try:
        with alvo.open("r", encoding="utf-8", errors="replace") as arquivo:
            for numero, linha in enumerate(arquivo, start=1):
                if numero < start_line:
                    continue
                if (
                    len(linhas) >= max_lines
                    or acumulado + len(linha) > MAX_TEXT_RESPONSE_SIZE
                ):
                    proxima = numero
                    break
                linhas.append({"linha": numero, "texto": linha.rstrip("\r\n")})
                acumulado += len(linha)
    except OSError as exc:
        raise ToolError(f"Não consegui ler {relative_path!r}: {exc}") from exc

    return _json(
        {
            "caminho": caminho_relativo(alvo),
            "linhas": linhas,
            "next_line": proxima,
        }
    )


# ============================================================
# TOOL: SEARCH PROJECT (L5 — índice FTS5 + varredura de reserva)
# ============================================================

def _busca_literal(consulta: str, alvo: Path, max_results: int) -> str:
    """Plano B da busca: lê os arquivos um a um (como era antes do L5).

    É o caminho do índice frio (banco recém-criado, scanner ainda não
    rodou) e da consulta sem nenhum termo indexável (só pontuação).
    Mais lento, mas nunca deixa a tool sem resposta.
    """
    alvo_busca = consulta.casefold()
    resultados = []
    lidos = 0
    truncado = False

    for caminho in iter_workspace_files(alvo):
        try:
            if caminho.stat().st_size > MAX_FILE_SIZE:
                continue
        except OSError:
            continue

        if not extensao_legivel(caminho):
            continue

        try:
            texto = caminho.read_text(encoding="utf-8", errors="ignore")
        except (OSError, UnicodeError):
            continue

        lidos += 1

        if alvo_busca in texto.casefold():
            numero, trecho = localizar_trecho(texto, consulta)
            item = {"path": caminho_relativo(caminho)}
            if numero is not None:
                item["linha"] = numero
                item["trecho"] = trecho
            resultados.append(item)
            if len(resultados) >= max_results:
                truncado = True
                break

    resposta = {
        "consulta": consulta,
        "motor": "varredura_literal",
        "resultados": resultados,
        "arquivos_lidos": lidos,
        "truncado": truncado,
        "observacao": (
            "Resposta sem o índice de busca (frio, ou consulta só de "
            "pontuação). O scanner aquece o índice na próxima varredura."
        ),
    }
    if truncado:
        resposta["como_continuar"] = (
            "Limite atingido. Reduza o escopo com relative_path ou refine "
            "a consulta."
        )
    return _json(resposta)


@mcp.tool(annotations=ANOTACOES_LEITURA)
@audit.auditar
def search_project(
    query: str,
    relative_path: str = ".",
    max_results: int = 20,
    offset: int = 0,
) -> str:
    """Busca texto no código do workspace; devolve path, linha e trecho.

    Usa o índice FTS5 mantido por watcher e scanner (ranking BM25) — não
    relê arquivo nenhum. Pagine reenviando o `next_offset` em `offset`.
    Restrinja com `relative_path` (ex.: 'src/servicos'). Com o índice
    frio a busca cai para a varredura literal e avisa em `motor`.
    """
    if not query or not query.strip():
        raise ToolError(
            "A busca não pode estar vazia — informe o texto procurado."
        )

    try:
        alvo = resolver(relative_path)
    except Exception as exc:
        raise _erro_de_caminho(exc, relative_path) from exc

    if not alvo.exists() or not alvo.is_dir():
        raise ToolError(
            f"{relative_path!r} não é um diretório do workspace. "
            f"Use '.' para buscar na raiz."
        )

    motivo = motivo_privacidade(alvo)
    if motivo is not None:
        raise ToolError(f"Busca bloqueada em {relative_path!r}: {motivo}.")
    if esta_ignorado(alvo):
        raise ToolError(
            f"{relative_path!r} é pasta gerada/ignorada e fica fora da busca."
        )

    consulta = query.strip()
    max_results = max(1, min(max_results, 100))
    offset = max(0, offset)

    # Sem termo indexável (só pontuação) o FTS não tem o que casar; índice
    # vazio idem — nos dois casos, é a varredura literal que responde.
    if not termos_da_consulta(consulta) or database.fts_total() == 0:
        return _busca_literal(consulta, alvo, max_results)

    prefixo = None if alvo == GUMI_ROOT else caminho_relativo(alvo)

    try:
        linhas, total = database.fts_buscar(
            frase_para_match(consulta), prefixo, max_results, offset
        )
    except sqlite3.OperationalError:
        # Consulta que o FTS5 não digere — o plano B responde do mesmo jeito.
        return _busca_literal(consulta, alvo, max_results)

    resultados = []
    for linha in linhas:
        numero, trecho = localizar_trecho(linha["conteudo"], consulta)
        item = {"path": linha["path"]}
        if numero is not None:
            item["linha"] = numero
            item["trecho"] = trecho
        resultados.append(item)

    fim = offset + len(resultados)
    proximo = fim if fim < total else None

    resposta = {
        "consulta": consulta,
        "motor": "fts5",
        "total": total,
        "offset": offset,
        "resultados": resultados,
        "next_offset": proximo,
    }
    if proximo is not None:
        resposta["como_continuar"] = (
            f"Ainda há {total - proximo} resultado(s): chame de novo com "
            f"offset={proximo}."
        )
    return _json(resposta)


# ============================================================
# TOOL: FILE METADATA
# ============================================================

@mcp.tool(annotations=ANOTACOES_LEITURA)
@audit.auditar
def get_file_metadata(relative_path: str, include_sha256: bool = False) -> str:
    """Tamanho, data de modificação e (opcionalmente) SHA-256 de um arquivo.

    O hash só é calculado com `include_sha256=true`, e nunca em arquivo acima do
    limite de tamanho.
    """
    try:
        alvo = resolver(relative_path)
    except Exception as exc:
        raise _erro_de_caminho(exc, relative_path) from exc

    if not alvo.exists():
        raise ToolError(f"Arquivo não encontrado: {relative_path!r}.")

    if not alvo.is_file():
        raise ToolError(f"{relative_path!r} é um diretório, não um arquivo.")

    motivo = motivo_privacidade(alvo)
    if motivo is not None:
        raise ToolError(
            f"Metadados bloqueados para {relative_path!r}: {motivo}."
        )

    estatistica = alvo.stat()

    return _json(
        {
            "caminho": caminho_relativo(alvo),
            "bytes": estatistica.st_size,
            "modificado_em": estatistica.st_mtime,
            "sha256": file_sha256(alvo) if include_sha256 else None,
        }
    )


# ============================================================
# TOOL: RECENT CHANGES
# ============================================================

@mcp.tool(annotations=ANOTACOES_LEITURA)
@audit.auditar
def get_recent_changes(limit: int = 50, event_type: str | None = None) -> str:
    """Últimas alterações vistas pelo watcher (mais antigas primeiro).

    Filtre com `event_type`: created, modified, deleted ou moved. Entre
    duas consultas eventos podem escapar — para polling sem perda, use
    get_changes_since.
    """
    tipos = {"created", "modified", "deleted", "moved"}
    if event_type is not None and event_type not in tipos:
        raise ToolError(
            f"event_type inválido: {event_type!r}. "
            f"Use um de: {', '.join(sorted(tipos))}."
        )

    return _json(database.recent_changes(limit, event_type))


# ============================================================
# TOOL: STORED FILE STATE
# ============================================================

@mcp.tool(annotations=ANOTACOES_LEITURA)
@audit.auditar
def get_stored_file_state(relative_path: str) -> str:
    """Último estado persistido de um arquivo (tamanho, mtime, hash), sem ler o disco."""
    try:
        alvo = resolver(relative_path)
    except Exception as exc:
        raise _erro_de_caminho(exc, relative_path) from exc

    motivo = motivo_privacidade(alvo)
    if motivo is not None:
        raise ToolError(
            f"Estado bloqueado para {relative_path!r}: {motivo}."
        )

    relativo = caminho_relativo(alvo)
    estado = database.get_file_state(relativo)

    if estado is None:
        return _json(
            {
                "encontrado": False,
                "caminho": relativo,
                "dica": (
                    "Arquivo não está no índice. Rode refresh_project_state "
                    "no diretório dele, ou confira se existe com list_files."
                ),
            }
        )

    return _json({"encontrado": True, **estado})


@mcp.tool(annotations=ANOTACOES_LEITURA)
@audit.auditar
def get_project_health(largest_files_limit: int = 10) -> str:
    """Saúde do servidor: watcher vivo?, último scan, registro de erros e índice.

    `watcher` e `scanner` só ficam ativos quando o servidor sobe por stdio
    (server.py como programa); `registro` resume o log e a trilha de auditoria.
    """
    maiores = [
        registro for registro in database.largest_files(largest_files_limit * 3)
        if motivo_privacidade(GUMI_ROOT / registro["path"]) is None
    ][:largest_files_limit]

    watcher_info = _info_watcher()
    scanner_info = _info_scanner()

    return _json(
        {
            "servidor": SERVER_NAME,
            "versao": SERVER_VERSION,
            "workspace": str(GUMI_ROOT),
            "watcher": watcher_info,
            "scanner": scanner_info,
            "registro": audit.resumo(),
            "estado": database.get_state_summary(),
            "indice_busca": {"documentos_indexados": database.fts_total()},
            "maiores_arquivos": maiores,
            "diretorios_ignorados": sorted(IGNORE_DIRECTORIES),
            "tamanho_maximo_indexado": MAX_FILE_SIZE,
        }
    )


@mcp.tool(annotations=ANOTACOES_LEITURA)
@audit.auditar
def optimize_state_database(retain_changes: int = 5_000, vacuum: bool = False) -> str:
    """Apaga histórico antigo e compacta o banco do MCP.

    Mexe só em data/gumi_state.db. Nunca toca em arquivo do workspace.
    """
    try:
        return _json(database.optimize(retain_changes, vacuum))
    except Exception as exc:
        raise ToolError(f"Falha ao otimizar o banco de estado: {exc}") from exc


@mcp.tool(annotations=ANOTACOES_LEITURA)
@audit.auditar
def reconcile_project_state() -> str:
    """Marca como removido o que está no índice mas não existe mais no disco.

    Útil depois do primeiro índice, se o watcher ficou desligado, ou quando as
    regras de exclusão mudaram. Não calcula hash nem altera arquivo.
    """
    try:
        atuais = {caminho_relativo(caminho) for caminho in iter_workspace_files(GUMI_ROOT)}
    except OSError as exc:
        raise ToolError(f"Não consegui varrer o workspace: {exc}") from exc

    conhecidos = database.get_all_paths()
    ativos = database.get_active_paths()

    # Duas situações diferentes, dois tratamentos diferentes:
    #
    # 1. o caminho é bloqueado pela política -> a linha é APAGADA. Guardar o
    #    nome de um arquivo de memória, mesmo marcado como removido, ainda é
    #    guardar dado pessoal.
    # 2. o arquivo só sumiu do disco -> marcado como removido, porque esse
    #    histórico é útil.
    proibidos = sorted(
        caminho for caminho in conhecidos
        if not pode_indexar(GUMI_ROOT / caminho)
    )
    apagados = database.delete_states(proibidos)

    sumidos = sorted((ativos - atuais) - set(proibidos))
    marcados = database.mark_files_deleted(sumidos)

    return _json(
        {
            "arquivos_no_disco": len(atuais),
            "indexados_antes": len(ativos),
            "apagados_por_privacidade": apagados,
            "marcados_como_removidos": marcados,
            "indexados_depois": len(database.get_active_paths()),
            "observacao": (
                "Caminho bloqueado pela política sai do índice de vez; arquivo "
                "que só deixou de existir fica marcado como removido."
            ),
        }
    )


# ============================================================
# TOOL: REFRESH PROJECT STATE
# ============================================================

@mcp.tool(annotations=ANOTACOES_LEITURA)
@audit.auditar
def refresh_project_state(
    relative_path: str = ".",
    max_files: int = DEFAULT_REFRESH_BATCH_SIZE,
    start_after: str | None = None,
) -> str:
    """Atualiza um pedaço do índice, em blocos, sem estourar o tempo da chamada.

    Reenvie o `next_cursor` recebido em `start_after` até ele vir nulo. Para
    indexar só uma área, informe `relative_path` (ex.: 'gumi').
    """
    try:
        alvo = resolver(relative_path)
    except Exception as exc:
        raise _erro_de_caminho(exc, relative_path) from exc

    if not alvo.exists() or not alvo.is_dir():
        raise ToolError(
            f"{relative_path!r} não é um diretório do workspace. Use '.' para a raiz."
        )

    max_files = max(1, min(max_files, MAX_REFRESH_BATCH_SIZE))

    caminhos = sorted(
        iter_workspace_files(alvo),
        key=lambda caminho: caminho_relativo(caminho).casefold(),
    )
    relativos = [caminho_relativo(caminho) for caminho in caminhos]

    if start_after:
        try:
            inicio = relativos.index(start_after) + 1
        except ValueError:
            # O cursor pode apontar para arquivo removido entre duas chamadas.
            corte = start_after.casefold()
            inicio = 0
            while inicio < len(relativos) and relativos[inicio].casefold() <= corte:
                inicio += 1
    else:
        inicio = 0

    lote = caminhos[inicio : inicio + max_files]
    relativos_lote = [caminho_relativo(caminho) for caminho in lote]
    anteriores = database.get_existing_states(relativos_lote)

    pendentes = []
    para_busca = []
    vistos = 0
    atualizados = 0
    inalterados = 0
    grandes = 0
    erros = 0

    for caminho, relativo in zip(lote, relativos_lote):
        try:
            estatistica = caminho.stat()

            if estatistica.st_size > MAX_FILE_SIZE:
                grandes += 1
                continue

            vistos += 1
            anterior = anteriores.get(relativo)

            if (
                anterior
                and anterior["size"] == estatistica.st_size
                and anterior["modified_at"] == estatistica.st_mtime
            ):
                inalterados += 1
                pendentes.append(
                    (
                        relativo,
                        anterior["size"],
                        anterior["modified_at"],
                        anterior["sha256"],
                        True,
                    )
                )
                continue

            pendentes.append(
                (
                    relativo,
                    estatistica.st_size,
                    estatistica.st_mtime,
                    file_sha256(caminho),
                    True,
                )
            )
            atualizados += 1
            para_busca.append((caminho, relativo))

        except (OSError, ValueError):
            erros += 1

    database.update_file_states(pendentes)

    # L5: quem teve conteúdo atualizado entra também no índice de busca.
    lote_busca = []
    for caminho, relativo in para_busca:
        texto = texto_para_indexar(caminho)
        if texto is not None:
            lote_busca.append((relativo, texto))
    database.fts_indexar_lote(lote_busca)

    restantes = len(caminhos) - inicio - len(lote)
    proximo = relativos_lote[-1] if restantes > 0 and relativos_lote else None

    resposta = {
        "workspace": str(GUMI_ROOT),
        "vistos": vistos,
        "atualizados": atualizados,
        "inalterados": inalterados,
        "ignorados_por_tamanho": grandes,
        "erros": erros,
        "indexados_para_busca": len(lote_busca),
        "neste_lote": len(lote),
        "restantes": max(0, restantes),
        "next_cursor": proximo,
    }

    if proximo:
        resposta["como_continuar"] = (
            f"Chame de novo com relative_path={relative_path!r} e "
            f"start_after={proximo!r}."
        )

    return _json(resposta)


# ============================================================
# TOOLS: OLHOS E REGISTRO (L3)
# ============================================================

@mcp.tool(annotations=ANOTACOES_LEITURA)
@audit.auditar
def get_server_log(max_lines: int = 100) -> str:
    """Últimas linhas do log do servidor (logs/servidor.log).

    Watcher, scanner e falhas internas escrevem aqui — nunca em stdout. Os
    arquivos rotacionados (.1, .2, ...) ficam no disco, fora desta resposta.
    """
    max_lines = max(1, min(max_lines, 500))
    arquivo = audit.caminho_log()
    linhas = audit.ler_linhas_finais(arquivo, max_lines)

    resposta = {
        "arquivo": str(arquivo),
        "linhas": linhas,
        "erros_desde_inicio": audit.resumo()["erros_desde_inicio"],
    }
    if not linhas:
        resposta["observacao"] = (
            "Nada registrado ainda nesta execução — o log nasce no primeiro "
            "evento de watcher, scanner ou erro interno."
        )
    return _json(resposta)


@mcp.tool(annotations=ANOTACOES_LEITURA)
@audit.auditar
def get_audit_trail(
    limit: int = 50,
    tool: str | None = None,
    only_errors: bool = False,
) -> str:
    """Trilha de auditoria: cada chamada de tool, com argumentos e desfecho.

    Uma entrada por chamada (logs/auditoria.jsonl), mais antigas primeiro.
    Filtre por `tool` (ex.: 'read_file') ou só falhas com `only_errors=true`.
    É a resposta para "o que consultaram sobre a Gumi?".
    """
    limit = max(1, min(limit, 500))
    arquivo = audit.caminho_auditoria()

    entradas = []
    invalidas = 0
    for linha in audit.ler_linhas_finais(arquivo, 2_000):
        try:
            entrada = json.loads(linha)
        except json.JSONDecodeError:
            invalidas += 1
            continue
        if tool is not None and entrada.get("tool") != tool:
            continue
        if only_errors and entrada.get("sucesso", True):
            continue
        entradas.append(entrada)

    resposta = {
        "arquivo": str(arquivo),
        "entradas": entradas[-limit:],
        "linhas_invalidas": invalidas,
    }
    if not entradas:
        resposta["observacao"] = (
            "Nenhuma chamada registrada com esses filtros."
            if (tool or only_errors)
            else "Nenhuma chamada registrada ainda nesta execução."
        )
    return _json(resposta)


# ============================================================
# TOOLS: CONTRATO PARA AGENTES (L4)
# ============================================================

def _cursor_texto(instancia: str, ultimo_id: int) -> str:
    return f"{instancia}:{ultimo_id}"


def _resposta_fresca(motivo: str) -> str:
    """Resposta `fresh_instance` (modelo Watchman): o cursor não vale mais.

    Não devolve evento nenhum — devolve um cursor novo e a instrução de
    refazer a baseline, para o cliente nunca agir sobre um buraco no fluxo.
    """
    return _json(
        {
            "fresh_instance": True,
            "changes": [],
            "next_cursor": _cursor_texto(
                database.get_instance_id(), database.last_change_id()
            ),
            "motivo": motivo,
            "como_continuar": (
                "Guarde o next_cursor e refaça sua visão do workspace "
                "(get_project_state e, se precisar, refresh_project_state); "
                "depois volte a consultar com o cursor."
            ),
        }
    )


@mcp.tool(annotations=ANOTACOES_LEITURA)
@audit.auditar
def get_changes_since(
    cursor: str | None = None,
    limit: int = 200,
    response_format: str = "detailed",
) -> str:
    """Mudanças desde um cursor, sem perder nem repetir evento (polling barato).

    Primeira chamada: sem cursor — devolve `fresh_instance=true` e o
    `next_cursor` inicial. Depois, reenvie sempre o último `next_cursor`.
    `fresh_instance=true` significa fluxo rompido (índice recriado ou
    histórico compactado): refaça a baseline antes de confiar nos eventos.
    Diferente de get_recent_changes, nada escapa entre consultas.
    `response_format='concise'` devolve só id, tipo e caminho.
    """
    if response_format not in ("concise", "detailed"):
        raise ToolError(
            f"response_format inválido: {response_format!r}. "
            f"Use 'concise' ou 'detailed'."
        )
    limit = max(1, min(limit, 500))

    if cursor is None or not cursor.strip():
        return _resposta_fresca("primeira consulta: baseline estabelecida agora.")

    partes = cursor.strip().split(":")
    if len(partes) != 2 or not partes[1].isdigit():
        raise ToolError(
            f"Cursor inválido: {cursor!r}. O formato é 'instancia:numero', "
            f"sempre copiado do next_cursor da resposta anterior. Para "
            f"começar do zero, chame get_changes_since sem cursor."
        )
    instancia_pedida, id_pedido = partes[0], int(partes[1])

    instancia = database.get_instance_id()
    ultimo = database.last_change_id()

    if instancia_pedida != instancia:
        return _resposta_fresca(
            "o índice foi recriado desde esse cursor (outra instância)."
        )
    if id_pedido > ultimo:
        return _resposta_fresca(
            "o cursor aponta para além do histórico atual (índice restaurado?)."
        )

    mais_antigo = database.oldest_change_id()
    historico_comeca_em = mais_antigo - 1 if mais_antigo is not None else ultimo
    if id_pedido < historico_comeca_em:
        return _resposta_fresca(
            "o histórico foi compactado e eventos dessa janela se perderam."
        )

    linhas = database.changes_since(id_pedido, limit)

    if response_format == "concise":
        changes = []
        for linha in linhas:
            evento = {
                "id": linha["id"],
                "event_type": linha["event_type"],
                "path": linha["path"],
            }
            if linha["old_path"]:
                evento["old_path"] = linha["old_path"]
            changes.append(evento)
    else:
        changes = linhas

    novo_id = linhas[-1]["id"] if linhas else id_pedido
    resposta = {
        "fresh_instance": False,
        "changes": changes,
        "next_cursor": _cursor_texto(instancia, novo_id),
        "restantes_aproximado": max(0, ultimo - novo_id),
    }
    if novo_id < ultimo:
        resposta["como_continuar"] = (
            f"Ainda há eventos: chame de novo com "
            f"cursor={_cursor_texto(instancia, novo_id)!r}."
        )
    return _json(resposta)


@mcp.tool(annotations=ANOTACOES_LEITURA)
@audit.auditar
def verify_integrity(
    relative_path: str = ".",
    check_hash: bool = False,
    max_listed: int = 50,
    response_format: str = "detailed",
) -> str:
    """Compara o disco com a baseline do índice: iguais/modificados/sumidos/novos.

    Modelo AIDE/Tripwire. Por padrão compara tamanho e data de modificação;
    `check_hash=true` rehasheia quem parece igual, pegando alteração que
    preservou os metadados. `response_format='concise'` devolve só as
    contagens. Nada no workspace é modificado.
    """
    if response_format not in ("concise", "detailed"):
        raise ToolError(
            f"response_format inválido: {response_format!r}. "
            f"Use 'concise' ou 'detailed'."
        )
    max_listed = max(1, min(max_listed, 500))

    try:
        alvo = resolver(relative_path)
    except Exception as exc:
        raise _erro_de_caminho(exc, relative_path) from exc

    motivo = motivo_privacidade(alvo)
    if motivo is not None:
        raise ToolError(
            f"Verificação bloqueada para {relative_path!r}: {motivo}."
        )
    if alvo.exists() and esta_ignorado(alvo):
        raise ToolError(
            f"{relative_path!r} é pasta gerada/ignorada e fica fora do índice."
        )

    raiz_do_escopo = alvo == GUMI_ROOT

    # O que existe agora no disco (já filtrado pela política)…
    if alvo.is_file():
        atuais = {caminho_relativo(alvo): alvo} if pode_indexar(alvo) else {}
    elif alvo.is_dir():
        atuais = {caminho_relativo(c): c for c in iter_workspace_files(alvo)}
    else:
        atuais = {}

    # …e o que a baseline conhecia dentro do mesmo escopo. Caminho que não
    # existe mais no disco continua entrando aqui — vira 'sumido'.
    if raiz_do_escopo:
        relativo_escopo = "."
        baseline = database.get_active_paths()
    else:
        relativo_escopo = caminho_relativo(alvo)
        baseline = set(database.get_active_paths_under(relativo_escopo))

    if not baseline and not atuais:
        raise ToolError(
            f"Nada em {relative_path!r}: nem no disco nem na baseline. "
            f"Confira o caminho com list_files ou indexe com "
            f"refresh_project_state."
        )

    anteriores = database.get_existing_states(sorted(atuais))

    novos = sorted(rel for rel in atuais if rel not in baseline)
    sumidos = sorted(baseline - set(atuais))

    iguais = 0
    modificados = []
    sem_hash_na_baseline = 0
    grandes_sem_rehash = 0
    erros = 0

    for rel in sorted(atuais):
        if rel not in baseline:
            continue
        anterior = anteriores.get(rel)
        if anterior is None:
            erros += 1
            continue
        try:
            estatistica = atuais[rel].stat()
        except OSError:
            erros += 1
            continue

        if (
            anterior["size"] != estatistica.st_size
            or anterior["modified_at"] != estatistica.st_mtime
        ):
            modificados.append({"path": rel, "motivo": "tamanho_ou_data"})
            continue

        if check_hash:
            if anterior["sha256"] is None:
                sem_hash_na_baseline += 1
                iguais += 1
                continue
            if estatistica.st_size > MAX_FILE_SIZE:
                grandes_sem_rehash += 1
                iguais += 1
                continue
            if file_sha256(atuais[rel]) != anterior["sha256"]:
                modificados.append({"path": rel, "motivo": "conteudo"})
                continue

        iguais += 1

    resposta = {
        "escopo": relativo_escopo,
        "verificado_com": (
            "tamanho+data+sha256" if check_hash else "tamanho+data"
        ),
        "contagem": {
            "iguais": iguais,
            "modificados": len(modificados),
            "sumidos": len(sumidos),
            "novos": len(novos),
        },

        "baseline_arquivos": len(baseline),
        "sem_hash_na_baseline": sem_hash_na_baseline,
        "grandes_sem_rehash": grandes_sem_rehash,
        "erros_de_leitura": erros,
        "observacao": (
            "A baseline é o índice persistente. 'novos' e 'modificados' "
            "viram baseline ao rodar refresh_project_state; nada foi "
            "alterado aqui."
        ),
    }

    if response_format == "detailed":
        resposta["modificados"] = modificados[:max_listed]
        resposta["sumidos"] = sumidos[:max_listed]
        resposta["novos"] = novos[:max_listed]
        if (
            len(modificados) > max_listed
            or len(sumidos) > max_listed
            or len(novos) > max_listed
        ):
            resposta["como_continuar"] = (
                "Listas cortadas em max_listed. Aumente max_listed ou "
                "restrinja o escopo com relative_path."
            )

    return _json(resposta)


# ============================================================
# RESOURCES E PROMPTS (L6) — de ferramenta para órgão
# ============================================================
#
# Resources: contexto que o cliente (a Gumi inclusive) puxa direto, sem
# gastar uma chamada de tool. Prompts: roteiros prontos de uso do
# servidor. Os dois passam pela mesma auditoria das tools (L3): o pedido
# fica na trilha, o conteúdo servido nunca.

@mcp.resource(
    "gumi://workspace/estado",
    name="estado_do_workspace",
    title="Estado do workspace da Gumi",
    description=(
        "Resumo do índice persistente (arquivos ativos, removidos, últimos "
        "eventos), sem varrer o disco. Mesmo conteúdo de get_project_state."
    ),
    mime_type="application/json",
)
@audit.auditar
def recurso_estado() -> str:
    """gumi://workspace/estado — o corpo, em números, direto do índice."""
    return _json({"workspace": str(GUMI_ROOT), **database.get_state_summary()})


@mcp.resource(
    "gumi://workspace/saude",
    name="saude_do_servidor",
    title="Saúde do gumi-mcp",
    description=(
        "Watcher vivo?, último scan, registro de erros e tamanho do índice "
        "de busca — a versão enxuta de get_project_health."
    ),
    mime_type="application/json",
)
@audit.auditar
def recurso_saude() -> str:
    """gumi://workspace/saude — o servidor está enxergando direito?"""
    return _json(
        {
            "servidor": SERVER_NAME,
            "versao": SERVER_VERSION,
            "workspace": str(GUMI_ROOT),
            "watcher": _info_watcher(),
            "scanner": _info_scanner(),
            "registro": audit.resumo(),
            "estado": database.get_state_summary(),
            "indice_busca": {"documentos_indexados": database.fts_total()},
        }
    )


@mcp.prompt(
    name="perceber_o_corpo",
    title="Perceber o próprio corpo",
    description=(
        "Primeiro contato com o workspace: montar a baseline e guardar o "
        "cursor, sem varrer nada. Escrito para a Gumi, serve para qualquer "
        "agente."
    ),
)
@audit.auditar
def prompt_perceber_o_corpo() -> str:
    return (
        "Você está conectada ao gumi-mcp, o servidor SOMENTE LEITURA que "
        "expõe o workspace da Gumi — o corpo dela: código e arquivos. Monte "
        "sua primeira visão nesta ordem:\n"
        "1. Leia o resource gumi://workspace/estado (ou get_project_state): "
        "é o índice persistente, sem varredura.\n"
        "2. Chame get_changes_since sem cursor e GUARDE o next_cursor — é "
        "sua âncora para nunca perder nem repetir evento.\n"
        "3. Para conferir o índice contra o disco: verify_integrity('.').\n"
        "4. Para procurar qualquer coisa: search_project — a resposta já "
        "traz path, linha e trecho.\n"
        "Regras que não se negociam:\n"
        "- Tudo daqui é CONTEXTO OBSERVADO (fonte: workspace_observado). "
        "Nunca derive emoção ou classificação sobre a pessoa disto.\n"
        "- O que aparecer bloqueado/privado é desenho, não falha. Não tente "
        "contornar.\n"
        "- Nada aqui modifica arquivo: este servidor é um órgão de percepção."
    )


@mcp.prompt(
    name="acompanhar_mudancas",
    title="Acompanhar mudanças no workspace",
    description=(
        "Rotina de polling barato com cursor: o que mudou desde a última "
        "olhada, sem perder nem repetir evento."
    ),
)
@audit.auditar
def prompt_acompanhar_mudancas() -> str:
    return (
        "Rotina de acompanhamento do workspace da Gumi (polling barato):\n"
        "1. Use o next_cursor guardado da última consulta.\n"
        "2. Chame get_changes_since(cursor=...) e reenvie SEMPRE o novo "
        "next_cursor devolvido.\n"
        "3. fresh_instance=true significa fluxo rompido: refaça a baseline "
        "(get_project_state) antes de confiar em qualquer evento.\n"
        "4. Caminho importante? Aprofunde com get_stored_file_state e "
        "read_file_excerpt; conferência forte é verify_integrity com "
        "check_hash=true.\n"
        "Trate cada evento como observação datada do corpo — nunca como "
        "emoção ou julgamento sobre a pessoa."
    )


@mcp.prompt(
    name="investigar_arquivo",
    title="Investigar um arquivo do workspace",
    description=(
        "Roteiro de investigação de um arquivo: índice, disco e conteúdo, "
        "nessa ordem, tudo somente leitura."
    ),
)
@audit.auditar
def prompt_investigar_arquivo(relative_path: str) -> str:
    return (
        f"Investigue o arquivo {relative_path!r} do workspace da Gumi, "
        "somente leitura:\n"
        f"1. get_stored_file_state(relative_path={relative_path!r}) — o que "
        "o índice sabe.\n"
        f"2. get_file_metadata(relative_path={relative_path!r}, "
        "include_sha256=true) — o disco, agora.\n"
        f"3. read_file_excerpt(relative_path={relative_path!r}, "
        "start_line=1) — o conteúdo, por trechos.\n"
        "4. search_project(query='<nome ou símbolo>') para achar quem usa.\n"
        "Se qualquer passo voltar bloqueado por privacidade, pare ali: é "
        "zona íntima, e contornar não é opção."
    )


# ============================================================
# ENTRADA
# ============================================================

def main() -> None:
    """Sobe o watcher e serve por stdio. Só aqui há efeito colateral (F10)."""
    global _watcher, _scanner

    if not GUMI_ROOT.exists():
        # stderr: em stdio, stdout pertence ao protocolo.
        audit.erro(f"workspace não encontrado: {GUMI_ROOT}")
        print(
            f"[gumi-mcp] Workspace não encontrado: {GUMI_ROOT}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    audit.info(
        f"{SERVER_NAME} {SERVER_VERSION} subindo — workspace: {GUMI_ROOT}"
    )

    _watcher = GumiWatcher(root=GUMI_ROOT, database=database)
    _watcher.start()

    # L2: varredura de reconciliação na subida e a cada N horas — pega o que
    # mudou enquanto o servidor esteve desligado (modelo Wazuh FIM).
    _scanner = ScannerPeriodico(database)
    _scanner.start()

    try:
        mcp.run()
    finally:
        _scanner.stop()
        _watcher.stop()
        audit.info(f"{SERVER_NAME} desligado.")


if __name__ == "__main__":
    main()
