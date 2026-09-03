"""Watcher em tempo real do workspace da Gumi.

Usa a mesma política de `security.py` que as tools: o que não pode ser servido
também não entra no índice — nem como metadado. Um caminho dentro da zona
íntima nunca vira linha no banco.
"""

import time
from datetime import datetime, timezone
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

import audit
from config import WATCHER_DEBOUNCE_SECONDS
from database import GumiDatabase
from fulltext import texto_para_indexar
from hashing import sha256_do_arquivo
from security import caminho_relativo, iterar_indexaveis, pode_indexar

# Nome antigo, mantido para não quebrar imports existentes (L2: hash unificado).
calculate_sha256 = sha256_do_arquivo


class GumiEventHandler(FileSystemEventHandler):
    def __init__(self, root: Path, database: GumiDatabase):
        self.root = root
        self.database = database
        self._recentes: dict[tuple[str, str, str | None], float] = {}

    # --------------------------------------------------------
    # Apoio
    # --------------------------------------------------------

    def _relativo(self, caminho: str) -> str:
        return caminho_relativo(Path(caminho).resolve())

    def _relevante(self, caminho: str) -> bool:
        """Só nos interessa o que a política deixa indexar."""
        return pode_indexar(Path(caminho))

    def _registrar_evento(
        self,
        tipo: str,
        caminho: str,
        caminho_antigo: str | None = None,
    ) -> str:
        relativo = self._relativo(caminho)
        antigo = self._relativo(caminho_antigo) if caminho_antigo else None

        chave = (tipo, relativo, antigo)
        agora = time.monotonic()
        visto = self._recentes.get(chave)

        if visto is not None and agora - visto < WATCHER_DEBOUNCE_SECONDS:
            return relativo

        self._recentes[chave] = agora

        if len(self._recentes) > 2_000:
            self._recentes = {
                k: quando for k, quando in self._recentes.items()
                if agora - quando < WATCHER_DEBOUNCE_SECONDS
            }

        self.database.record_change(
            event_type=tipo,
            path=relativo,
            old_path=antigo,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        return relativo

    def _atualizar_estado(self, caminho: str) -> None:
        arquivo = Path(caminho)

        if not arquivo.is_file():
            return

        try:
            estatistica = arquivo.stat()
            relativo = self._relativo(str(arquivo))
            anterior = self.database.get_existing_state(relativo)

            # Tamanho e mtime iguais: não vale recalcular o hash.
            if (
                anterior
                and anterior["size"] == estatistica.st_size
                and anterior["modified_at"] == estatistica.st_mtime
            ):
                self.database.update_file_state(
                    path=relativo,
                    size=anterior["size"],
                    modified_at=anterior["modified_at"],
                    sha256=anterior["sha256"],
                    exists_now=True,
                )
                return

            self.database.update_file_state(
                path=relativo,
                size=estatistica.st_size,
                modified_at=estatistica.st_mtime,
                sha256=sha256_do_arquivo(arquivo),
                exists_now=True,
            )

            # L5: conteúdo alterado entra no índice de busca junto com o
            # estado — o que não é texto liberado volta None e fica fora.
            texto = texto_para_indexar(arquivo)
            if texto is not None:
                self.database.fts_indexar(relativo, texto)

        except (OSError, ValueError) as exc:
            # F8/L3: erro engolido em silêncio virava índice desatualizado
            # sem ninguém saber. Agora vira linha de log.
            audit.aviso(
                f"watcher: não consegui atualizar o estado de {caminho!r}: {exc}"
            )

    # --------------------------------------------------------
    # Cascatas de diretório (F4) — no Windows, apagar ou mover uma
    # pasta chega como UM evento do diretório, sem eventos dos filhos.
    # Sem cascata, os filhos viram fantasmas eternos no índice.
    # --------------------------------------------------------

    def _apagar_arvore(self, relativo: str) -> None:
        """Marca como removidos todos os caminhos indexados sob um diretório."""
        filhos = self.database.get_active_paths_under(relativo)
        if not filhos:
            return
        self.database.mark_files_deleted(filhos)
        self.database.record_changes(
            [("deleted", filho, None) for filho in filhos]
        )

    def _indexar_arvore(self, raiz: Path) -> None:
        """Indexa um diretório que acabou de aparecer numa zona visível."""
        for arquivo in iterar_indexaveis(raiz):
            self._registrar_evento("created", str(arquivo))
            self._atualizar_estado(str(arquivo))

    def _mover_diretorio(self, event) -> None:
        origem_ok = self._relevante(event.src_path)
        destino_ok = self._relevante(event.dest_path)

        if not origem_ok and not destino_ok:
            return

        if origem_ok and destino_ok:
            antigo = self._relativo(event.src_path)
            novo = self._relativo(event.dest_path)
            pares = self.database.remap_prefix(antigo, novo)
            self.database.record_changes(
                [("moved", depois, antes) for antes, depois in pares]
                or [("moved", novo, antigo)]
            )
            return

        if origem_ok:
            # F3 em diretório: destino é zona ignorada/privada. Para o
            # índice, a árvore inteira deixou de existir — nada do destino
            # é registrado, nem como metadado.
            self._apagar_arvore(self._relativo(event.src_path))
            return

        # Veio de zona invisível para zona visível: os filhos são novidade.
        self._indexar_arvore(Path(event.dest_path))

    # --------------------------------------------------------
    # Eventos
    # --------------------------------------------------------

    def on_created(self, event):
        if event.is_directory or not self._relevante(event.src_path):
            return
        self._registrar_evento("created", event.src_path)
        self._atualizar_estado(event.src_path)

    def on_modified(self, event):
        if event.is_directory or not self._relevante(event.src_path):
            return
        self._registrar_evento("modified", event.src_path)
        self._atualizar_estado(event.src_path)

    def on_deleted(self, event):
        if event.is_directory:
            # F4: cascata — os filhos não geram eventos próprios no Windows.
            if self._relevante(event.src_path):
                self._apagar_arvore(self._relativo(event.src_path))
            return
        if not self._relevante(event.src_path):
            return
        relativo = self._registrar_evento("deleted", event.src_path)
        self.database.mark_file_deleted(relativo)

    def on_moved(self, event):
        if event.is_directory:
            self._mover_diretorio(event)
            return

        origem_relevante = self._relevante(event.src_path)
        destino_relevante = self._relevante(event.dest_path)

        if not origem_relevante and not destino_relevante:
            return

        if destino_relevante:
            self._registrar_evento(
                "moved",
                event.dest_path,
                caminho_antigo=event.src_path,
            )
        else:
            # F3: destino é zona ignorada/privada. Para o índice, o arquivo
            # simplesmente deixou de existir — nada do destino é registrado.
            self._registrar_evento("deleted", event.src_path)

        if origem_relevante:
            self.database.mark_file_deleted(self._relativo(event.src_path))

        if destino_relevante and Path(event.dest_path).exists():
            self._atualizar_estado(event.dest_path)


class GumiWatcher:
    def __init__(self, root: Path, database: GumiDatabase):
        self.root = root
        self.database = database
        self.observer = Observer()

    def start(self) -> None:
        handler = GumiEventHandler(root=self.root, database=self.database)
        self.observer.schedule(handler, str(self.root), recursive=True)
        self.observer.start()

    def stop(self) -> None:
        self.observer.stop()
        self.observer.join()
