"""Varredura de reconciliação do Gumi-MCP (L2).

Modelo Wazuh FIM: o watcher em tempo real e a varredura periódica se corrigem
mutuamente. O watcher perde o que acontece com o servidor desligado; a
varredura compara o disco com o índice e conserta — conteúdo alterado, arquivo
novo, arquivo sumido e caminho que a política de privacidade passou a bloquear.

Roda na inicialização do servidor e a cada SCAN_INTERVAL_HOURS, numa thread
própria. Nunca escreve em stdout: em stdio, stdout é o canal JSON-RPC —
resultado e falha de varredura vão para o log em arquivo (L3).
"""

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import audit
from config import GUMI_ROOT, MAX_FILE_SIZE, SCAN_INTERVAL_HOURS
from database import GumiDatabase
from fulltext import texto_para_indexar
from hashing import sha256_do_arquivo
from security import caminho_relativo, iterar_indexaveis, pode_indexar


def varrer(database: GumiDatabase, raiz: Path | None = None) -> dict:
    """Reconcilia o índice com o disco e registra o que mudou.

    Quatro descobertas possíveis, quatro tratamentos:
    - caminho bloqueado pela política -> linha APAGADA (nome já é dado pessoal);
    - arquivo novo no disco           -> indexado + evento 'created';
    - conteúdo alterado (size/mtime)  -> rehasheado + evento 'modified';
    - sumiu do disco                  -> marcado removido + evento 'deleted'.

    Só lê o workspace; só escreve no banco do próprio MCP.
    """
    raiz = GUMI_ROOT if raiz is None else raiz
    inicio = time.monotonic()

    atuais: dict[str, Path] = {
        caminho_relativo(caminho): caminho
        for caminho in iterar_indexaveis(raiz)
    }

    conhecidos = database.get_all_paths()
    ativos = database.get_active_paths()

    proibidos = sorted(
        caminho for caminho in conhecidos
        if not pode_indexar(raiz / caminho)
    )
    apagados = database.delete_states(proibidos)

    anteriores = database.get_existing_states(list(atuais))

    pendentes = []
    novos: list[str] = []
    modificados: list[str] = []
    grandes = 0
    erros = 0

    for relativo, caminho in atuais.items():
        try:
            estatistica = caminho.stat()
        except OSError:
            erros += 1
            continue

        if estatistica.st_size > MAX_FILE_SIZE:
            grandes += 1
            continue

        anterior = anteriores.get(relativo)
        if (
            anterior
            and anterior["size"] == estatistica.st_size
            and anterior["modified_at"] == estatistica.st_mtime
        ):
            continue  # inalterado: nem hash nem escrita

        pendentes.append(
            (
                relativo,
                estatistica.st_size,
                estatistica.st_mtime,
                sha256_do_arquivo(caminho),
                True,
            )
        )
        if anterior is None:
            novos.append(relativo)
        else:
            modificados.append(relativo)

    database.update_file_states(pendentes)

    # L5 — busca: conteúdo novo/alterado entra no FTS, e o backfill cobre o
    # índice frio (banco criado antes de a busca existir). Binário, grande
    # demais ou extensão não liberada volta None e fica de fora.
    alterados = {pendente[0] for pendente in pendentes}
    ja_no_indice = database.fts_paths()
    lote_busca = []
    for relativo, caminho in atuais.items():
        if relativo in ja_no_indice and relativo not in alterados:
            continue
        texto = texto_para_indexar(caminho)
        if texto is not None:
            lote_busca.append((relativo, texto))
    database.fts_indexar_lote(lote_busca)

    sumidos = sorted((ativos - set(atuais)) - set(proibidos))
    removidos = database.mark_files_deleted(sumidos)

    database.record_changes(
        [("created", caminho, None) for caminho in novos]
        + [("modified", caminho, None) for caminho in modificados]
        + [("deleted", caminho, None) for caminho in sumidos]
    )

    return {
        "arquivos_no_disco": len(atuais),
        "novos": len(novos),
        "modificados": len(modificados),
        "removidos": removidos,
        "apagados_por_privacidade": apagados,
        "indexados_para_busca": len(lote_busca),
        "ignorados_por_tamanho": grandes,
        "erros": erros,
        "duracao_segundos": round(time.monotonic() - inicio, 2),
    }


class ScannerPeriodico:
    """Roda `varrer` na subida do servidor e depois a cada N horas.

    Thread daemon: não segura o desligamento. Resultado e falha de varredura
    vão para o log em arquivo (L3) — nunca para stdout.
    """

    def __init__(
        self,
        database: GumiDatabase,
        intervalo_horas: float = SCAN_INTERVAL_HOURS,
    ):
        self.database = database
        self.intervalo_horas = intervalo_horas
        self.ultimo_resultado: dict | None = None
        self.ultimo_scan_em: str | None = None
        self._parar = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._laco,
            name="gumi-mcp-scanner",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._parar.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def esta_vivo(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def executar_uma_vez(self) -> None:
        """Uma varredura, com desfecho registrado no log — sucesso ou falha."""
        try:
            self.ultimo_resultado = varrer(self.database)
            self.ultimo_scan_em = datetime.now(timezone.utc).isoformat()
            audit.info(
                f"varredura de reconciliação concluída: {self.ultimo_resultado}"
            )
        except Exception:  # noqa: BLE001 — a thread não pode morrer
            audit.erro("varredura de reconciliação falhou", exc_info=True)

    def _laco(self) -> None:
        while not self._parar.is_set():
            self.executar_uma_vez()
            if self._parar.wait(self.intervalo_horas * 3600):
                return
