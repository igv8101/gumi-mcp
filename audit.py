"""Olhos e registro do Gumi-MCP (L3).

O servidor que existe para verificar logs finalmente tem log (F8). Duas
saídas, nenhuma delas stdout — em stdio, stdout é o canal JSON-RPC e um
byte solto derruba a conexão:

* ``logs/servidor.log``    — log humano, rotativo (RotatingFileHandler).
* ``logs/auditoria.jsonl`` — trilha de auditoria: uma linha JSON por chamada
  de tool (tool, argumentos, duração, sucesso/erro). Recomendação explícita
  do OWASP para MCP, e a matéria-prima para a Gumi um dia perguntar
  "o que consultaram sobre mim?".

Tudo aqui é preguiçoso: importar o módulo não cria pasta nem arquivo (a
lição do F10). E registro é apoio, não carga: falha de log nunca derruba
uma tool — o pior caso vira uma linha em stderr.
"""

import functools
import inspect
import json
import logging
import sys
import threading
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config import LOG_DIRECTORY

NOME_LOG = "servidor.log"
NOME_AUDITORIA = "auditoria.jsonl"

_MAX_BYTES_POR_ARQUIVO = 2 * 1024 * 1024   # rotaciona em ~2 MB
_ARQUIVOS_DE_BACKUP = 3
_MAX_TEXTO_NA_TRILHA = 300                  # argumento gigante não vira linha gigante

_trava = threading.Lock()
_logger: logging.Logger | None = None
_trilha: logging.Logger | None = None
_contadores = {"erros": 0, "avisos": 0}
_iniciado_em = datetime.now(timezone.utc).isoformat()


def caminho_log() -> Path:
    return LOG_DIRECTORY / NOME_LOG


def caminho_auditoria() -> Path:
    return LOG_DIRECTORY / NOME_AUDITORIA


# ============================================================
# Configuração preguiçosa
# ============================================================

def _configurar(nome: str, arquivo: Path, formato: str) -> logging.Logger:
    """Prepara um logger de arquivo, limpando handlers de execuções antigas.

    O objeto de logger é global no processo (logging.getLogger); na suíte o
    módulo é reimportado com outro LOG_DIRECTORY, então handlers velhos são
    fechados para o registro não vazar para o diretório errado.
    """
    logger = logging.getLogger(nome)
    logger.setLevel(logging.INFO)
    logger.propagate = False  # nada sobe ao root (que escreve em stderr)

    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)

    arquivo.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        arquivo,
        maxBytes=_MAX_BYTES_POR_ARQUIVO,
        backupCount=_ARQUIVOS_DE_BACKUP,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(formato))
    logger.addHandler(handler)
    return logger


def _log() -> logging.Logger:
    global _logger
    with _trava:
        if _logger is None:
            _logger = _configurar(
                "gumi_mcp.servidor",
                caminho_log(),
                "%(asctime)s %(levelname)-7s %(message)s",
            )
    return _logger


def _auditoria() -> logging.Logger:
    global _trilha
    with _trava:
        if _trilha is None:
            _trilha = _configurar(
                "gumi_mcp.auditoria",
                caminho_auditoria(),
                "%(message)s",
            )
    return _trilha


def _seguro(acao) -> None:
    """Registrar nunca pode derrubar quem chamou; o pior caso vai a stderr."""
    try:
        acao()
    except Exception as exc:  # noqa: BLE001 — log é apoio, não carga
        print(f"[gumi-mcp] falha ao registrar log: {exc}", file=sys.stderr)


# ============================================================
# Log do servidor
# ============================================================

def info(mensagem: str) -> None:
    _seguro(lambda: _log().info(mensagem))


def aviso(mensagem: str) -> None:
    _contadores["avisos"] += 1
    _seguro(lambda: _log().warning(mensagem))


def erro(mensagem: str, exc_info: bool = False) -> None:
    _contadores["erros"] += 1
    _seguro(lambda: _log().error(mensagem, exc_info=exc_info))


def resumo() -> dict:
    """Estado do registro para o get_project_health — sem ler arquivo nenhum."""
    log = caminho_log()
    trilha = caminho_auditoria()
    return {
        "iniciado_em": _iniciado_em,
        "erros_desde_inicio": _contadores["erros"],
        "avisos_desde_inicio": _contadores["avisos"],
        "arquivo_log": str(log),
        "log_existe": log.exists(),
        "arquivo_auditoria": str(trilha),
        "auditoria_existe": trilha.exists(),
    }


def ler_linhas_finais(arquivo: Path, quantidade: int) -> list[str]:
    """Últimas N linhas de um arquivo de registro.

    Os arquivos rotacionam em ~2 MB, então ler o arquivo atual inteiro é
    barato; os rotacionados (.1, .2, ...) ficam no disco e fora da resposta.
    """
    if not arquivo.exists():
        return []
    texto = arquivo.read_text(encoding="utf-8", errors="replace")
    linhas = texto.splitlines()
    return linhas[-max(0, quantidade):] if quantidade else []


# ============================================================
# Trilha de auditoria (JSONL)
# ============================================================

def _resumir_argumentos(argumentos: dict) -> dict:
    """Achata os argumentos para a trilha: nunca conteúdo, só o que foi pedido."""
    resumidos: dict = {}
    for nome, valor in argumentos.items():
        if valor is None or isinstance(valor, (bool, int, float)):
            resumidos[nome] = valor
            continue
        texto = str(valor)
        if len(texto) > _MAX_TEXTO_NA_TRILHA:
            texto = texto[:_MAX_TEXTO_NA_TRILHA] + "…(truncado)"
        resumidos[nome] = texto
    return resumidos


def registrar_chamada(
    tool: str,
    argumentos: dict,
    duracao_ms: float,
    sucesso: bool,
    erro_texto: str | None = None,
) -> None:
    """Uma linha JSON por chamada de tool — o coração da trilha (OWASP)."""
    linha = {
        "em": datetime.now(timezone.utc).isoformat(),
        "tool": tool,
        "argumentos": _resumir_argumentos(argumentos),
        "duracao_ms": round(duracao_ms, 2),
        "sucesso": sucesso,
    }
    if erro_texto:
        linha["erro"] = erro_texto[: 2 * _MAX_TEXTO_NA_TRILHA]
    _seguro(lambda: _auditoria().info(json.dumps(linha, ensure_ascii=False)))


def auditar(funcao):
    """Decorador de tool: mede a duração e grava a chamada na trilha.

    A exceção da tool é re-levantada intacta (o ToolError com a mensagem em
    PT-BR segue chegando ao modelo); só o registro acontece no meio.
    """
    assinatura = inspect.signature(funcao)

    @functools.wraps(funcao)
    def envolvida(*args, **kwargs):
        try:
            argumentos = dict(assinatura.bind(*args, **kwargs).arguments)
        except TypeError:
            argumentos = dict(kwargs)

        inicio = time.perf_counter()
        try:
            resultado = funcao(*args, **kwargs)
        except Exception as exc:
            duracao_ms = (time.perf_counter() - inicio) * 1000
            registrar_chamada(
                funcao.__name__, argumentos, duracao_ms, False, str(exc)
            )
            raise
        duracao_ms = (time.perf_counter() - inicio) * 1000
        registrar_chamada(funcao.__name__, argumentos, duracao_ms, True)
        return resultado

    return envolvida
