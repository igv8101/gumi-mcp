import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from config import DATABASE_PATH


def _like_prefixo(prefixo: str) -> str:
    """Padrão LIKE para 'tudo dentro deste diretório', com curingas escapados.

    Caminhos reais têm '_' (ex.: 'gumi_old') e podem ter '%'. Sem escapar,
    'a_b/%' casaria com 'axb/...' e a cascata apagaria a árvore errada.
    """
    escapado = (
        prefixo.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    return escapado + "/%"


class GumiDatabase:
    def __init__(self, database_path: Path):
        self.database_path = database_path
        self.lock = Lock()

        self.database_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(
            self.database_path,
            check_same_thread=False,
        )

        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")

        return connection

    @contextmanager
    def _connection(self):
        """Abre, confirma e fecha cada conexão (importante no Windows)."""
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self):
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS file_state (
                    path TEXT PRIMARY KEY,
                    size INTEGER,
                    modified_at REAL,
                    sha256 TEXT,
                    last_seen TEXT,
                    exists_now INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    path TEXT NOT NULL,
                    old_path TEXT,
                    timestamp TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_changes_timestamp
                ON changes(timestamp);

                CREATE INDEX IF NOT EXISTS idx_changes_path
                ON changes(path);

                CREATE INDEX IF NOT EXISTS idx_file_state_exists
                ON file_state(exists_now);

                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                -- L5: índice de busca de conteúdo (BM25). Só entra o que a
                -- política deixa LER — decidido em fulltext.texto_para_indexar.
                CREATE VIRTUAL TABLE IF NOT EXISTS file_search USING fts5(
                    path UNINDEXED,
                    conteudo
                );
                """
            )
            # Identidade desta instância do índice (modelo Watchman): se o
            # banco for recriado, o id muda e todo cursor antigo vira
            # `fresh_instance` — o cliente sabe que precisa refazer a baseline.
            connection.execute(
                "INSERT OR IGNORE INTO meta (key, value) "
                "VALUES ('instance_id', ?)",
                (uuid.uuid4().hex,),
            )

    # ========================================================
    # CHANGE HISTORY
    # ========================================================

    def record_change(
        self,
        event_type: str,
        path: str,
        old_path: str | None = None,
        timestamp: str | None = None,
    ):
        if timestamp is None:
            timestamp = datetime.now(
                timezone.utc
            ).isoformat()

        with self.lock:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO changes (
                        event_type,
                        path,
                        old_path,
                        timestamp
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        event_type,
                        path,
                        old_path,
                        timestamp,
                    ),
                )

    def record_changes(self, eventos: list[tuple[str, str, str | None]]):
        """Grava vários eventos (tipo, caminho, caminho_antigo) de uma vez.

        Usado pelas cascatas de diretório (F4) e pelo scanner (L2): um rmdir
        com 300 filhos vira 300 linhas em uma transação, não 300 transações.
        """
        if not eventos:
            return
        timestamp = datetime.now(timezone.utc).isoformat()
        with self.lock:
            with self._connection() as connection:
                connection.executemany(
                    """
                    INSERT INTO changes (event_type, path, old_path, timestamp)
                    VALUES (?, ?, ?, ?)
                    """,
                    [
                        (tipo, caminho, antigo, timestamp)
                        for tipo, caminho, antigo in eventos
                    ],
                )

    def recent_changes(self, limit: int = 50, event_type: str | None = None):
        limit = max(
            1,
            min(limit, 500),
        )

        with self.lock:
            with self._connection() as connection:
                query = """
                    SELECT
                        id,
                        event_type,
                        path,
                        old_path,
                        timestamp
                    FROM changes
                """
                parameters: list[object] = []
                if event_type:
                    query += " WHERE event_type = ?"
                    parameters.append(event_type)
                query += " ORDER BY id DESC LIMIT ?"
                parameters.append(limit)
                rows = connection.execute(query, parameters).fetchall()

        return [
            dict(row)
            for row in reversed(rows)
        ]

    # ========================================================
    # FILE STATE
    # ========================================================

    def update_file_state(
        self,
        path: str,
        size: int | None,
        modified_at: float | None,
        sha256: str | None,
        exists_now: bool,
    ):
        timestamp = datetime.now(
            timezone.utc
        ).isoformat()

        with self.lock:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO file_state (
                        path,
                        size,
                        modified_at,
                        sha256,
                        last_seen,
                        exists_now
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(path)
                    DO UPDATE SET
                        size = excluded.size,
                        modified_at = excluded.modified_at,
                        sha256 = excluded.sha256,
                        last_seen = excluded.last_seen,
                        exists_now = excluded.exists_now
                    """,
                    (
                        path,
                        size,
                        modified_at,
                        sha256,
                        timestamp,
                        int(exists_now),
                    ),
                )

    def get_existing_states(self, paths: list[str]) -> dict[str, dict]:
        """Busca vários estados em poucas consultas SQLite."""
        if not paths:
            return {}

        states: dict[str, dict] = {}
        with self.lock:
            with self._connection() as connection:
                # SQLite aceita no máximo 999 parâmetros por consulta.
                for start in range(0, len(paths), 900):
                    chunk = paths[start : start + 900]
                    placeholders = ", ".join("?" for _ in chunk)
                    rows = connection.execute(
                        f"""
                        SELECT path, size, modified_at, sha256, last_seen, exists_now
                        FROM file_state
                        WHERE exists_now = 1 AND path IN ({placeholders})
                        """,
                        chunk,
                    ).fetchall()
                    states.update({row["path"]: dict(row) for row in rows})

        return states

    def update_file_states(self, states: list[tuple[str, int | None, float | None, str | None, bool]]):
        """Persiste um lote inteiro em uma única transação."""
        if not states:
            return

        timestamp = datetime.now(timezone.utc).isoformat()
        values = [
            (path, size, modified_at, sha256, timestamp, int(exists_now))
            for path, size, modified_at, sha256, exists_now in states
        ]

        with self.lock:
            with self._connection() as connection:
                connection.executemany(
                    """
                    INSERT INTO file_state (path, size, modified_at, sha256, last_seen, exists_now)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        size = excluded.size,
                        modified_at = excluded.modified_at,
                        sha256 = excluded.sha256,
                        last_seen = excluded.last_seen,
                        exists_now = excluded.exists_now
                    """,
                    values,
                )

    def mark_file_deleted(
        self,
        path: str,
    ):
        timestamp = datetime.now(
            timezone.utc
        ).isoformat()

        with self.lock:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO file_state (
                        path,
                        size,
                        modified_at,
                        sha256,
                        last_seen,
                        exists_now
                    )
                    VALUES (?, NULL, NULL, NULL, ?, 0)
                    ON CONFLICT(path)
                    DO UPDATE SET
                        last_seen = excluded.last_seen,
                        exists_now = 0
                    """,
                    (
                        path,
                        timestamp,
                    ),
                )
                connection.execute(
                    "DELETE FROM file_search WHERE path = ?",
                    (path,),
                )

    def mark_files_deleted(self, paths: list[str]) -> int:
        """Marca vários registros como removidos; devolve o rowcount REAL.

        Antes devolvia `len(paths)`, o que mentia quando o caminho nem estava
        ativo no índice. Agora só conta linha que de fato mudou.
        """
        if not paths:
            return 0
        timestamp = datetime.now(timezone.utc).isoformat()
        total = 0
        with self.lock:
            with self._connection() as connection:
                for inicio in range(0, len(paths), 900):
                    pedaco = paths[inicio : inicio + 900]
                    marcadores = ", ".join("?" for _ in pedaco)
                    cursor = connection.execute(
                        f"""
                        UPDATE file_state
                        SET size = NULL, modified_at = NULL, sha256 = NULL,
                            last_seen = ?, exists_now = 0
                        WHERE path IN ({marcadores}) AND exists_now = 1
                        """,
                        [timestamp, *pedaco],
                    )
                    total += cursor.rowcount or 0
                    connection.execute(
                        f"DELETE FROM file_search WHERE path IN ({marcadores})",
                        pedaco,
                    )
        return total

    def delete_states(self, paths: list[str]) -> int:
        """Apaga registros do índice de vez.

        Diferente de `mark_files_deleted`: aqui a linha some. É o que se faz com
        caminho que a política de privacidade passou a bloquear — deixar o
        registro marcado como removido ainda guardaria o NOME do arquivo, e
        nome de arquivo de memória ou de conversa já é dado pessoal.
        """
        if not paths:
            return 0

        with self.lock:
            with self._connection() as connection:
                total = 0
                for inicio in range(0, len(paths), 900):
                    pedaco = paths[inicio : inicio + 900]
                    marcadores = ", ".join("?" for _ in pedaco)
                    cursor = connection.execute(
                        f"DELETE FROM file_state WHERE path IN ({marcadores})",
                        pedaco,
                    )
                    total += cursor.rowcount or 0

                    cursor = connection.execute(
                        f"DELETE FROM changes WHERE path IN ({marcadores})",
                        pedaco,
                    )
                    connection.execute(
                        f"DELETE FROM file_search WHERE path IN ({marcadores})",
                        pedaco,
                    )
        return total

    def get_all_paths(self) -> set[str]:
        """Todo caminho conhecido, ativo ou não — usado para varrer o índice."""
        with self.lock:
            with self._connection() as connection:
                rows = connection.execute("SELECT path FROM file_state").fetchall()
        return {str(row["path"]) for row in rows}

    def get_active_paths(self) -> set[str]:
        with self.lock:
            with self._connection() as connection:
                rows = connection.execute(
                    "SELECT path FROM file_state WHERE exists_now = 1"
                ).fetchall()
        return {str(row["path"]) for row in rows}

    def get_active_paths_under(self, prefixo: str) -> list[str]:
        """Caminhos ativos dentro de um diretório (o prefixo em si incluído).

        É a pergunta que a cascata de diretório (F4) faz: "quem eu conhecia
        dentro desta pasta?" — no Windows, apagar/mover uma pasta chega ao
        watcher como UM evento do diretório, sem eventos dos filhos.
        """
        with self.lock:
            with self._connection() as connection:
                rows = connection.execute(
                    r"""
                    SELECT path FROM file_state
                    WHERE exists_now = 1
                      AND (path = ? OR path LIKE ? ESCAPE '\')
                    ORDER BY path
                    """,
                    (prefixo, _like_prefixo(prefixo)),
                ).fetchall()
        return [str(row["path"]) for row in rows]

    def remap_prefix(self, prefixo_antigo: str, prefixo_novo: str) -> list[tuple[str, str]]:
        """Renomeia um diretório no índice, filho por filho (F4, move).

        Devolve os pares (antigo, novo) para quem chamou registrar os eventos.
        O conteúdo não mudou num move, então tamanho, mtime e hash viajam
        junto — nada é rehasheado.
        """
        timestamp = datetime.now(timezone.utc).isoformat()
        with self.lock:
            with self._connection() as connection:
                rows = connection.execute(
                    r"""
                    SELECT path FROM file_state
                    WHERE exists_now = 1
                      AND (path = ? OR path LIKE ? ESCAPE '\')
                    ORDER BY path
                    """,
                    (prefixo_antigo, _like_prefixo(prefixo_antigo)),
                ).fetchall()

                pares = [
                    (
                        str(row["path"]),
                        prefixo_novo + str(row["path"])[len(prefixo_antigo):],
                    )
                    for row in rows
                ]

                for antigo, novo in pares:
                    # O destino pode já existir no índice (ex.: registro morto
                    # de um arquivo que um dia viveu lá) — a linha nova vence.
                    connection.execute(
                        "DELETE FROM file_state WHERE path = ?", (novo,)
                    )
                    connection.execute(
                        "UPDATE file_state SET path = ?, last_seen = ? WHERE path = ?",
                        (novo, timestamp, antigo),
                    )
                    # O índice de busca acompanha: num move o conteúdo não
                    # mudou, só o caminho.
                    connection.execute(
                        "DELETE FROM file_search WHERE path = ?", (novo,)
                    )
                    connection.execute(
                        "UPDATE file_search SET path = ? WHERE path = ?",
                        (novo, antigo),
                    )
        return pares

    def get_file_state(
        self,
        path: str,
    ):
        with self.lock:
            with self._connection() as connection:
                row = connection.execute(
                    """
                    SELECT
                        path,
                        size,
                        modified_at,
                        sha256,
                        last_seen,
                        exists_now
                    FROM file_state
                    WHERE path = ?
                    """,
                    (path,),
                ).fetchone()

        return (
            dict(row)
            if row
            else None
        )

    def get_current_file_count(self) -> int:
        with self.lock:
            with self._connection() as connection:
                row = connection.execute(
                    """
                    SELECT COUNT(*)
                    AS count
                    FROM file_state
                    WHERE exists_now = 1
                    """
                ).fetchone()

        return int(row["count"])

    def get_known_file_count(self) -> int:
        with self.lock:
            with self._connection() as connection:
                row = connection.execute(
                    """
                    SELECT COUNT(*)
                    AS count
                    FROM file_state
                    """
                ).fetchone()

        return int(row["count"])

    def get_state_summary(self):
        with self.lock:
            with self._connection() as connection:
                row = connection.execute(
                    """
                    SELECT
                        COUNT(*) AS known_files,
                        SUM(CASE WHEN exists_now = 1 THEN 1 ELSE 0 END) AS current_files,
                        SUM(CASE WHEN exists_now = 0 THEN 1 ELSE 0 END) AS deleted_files,
                        SUM(CASE WHEN exists_now = 1 THEN COALESCE(size, 0) ELSE 0 END) AS indexed_bytes,
                        MAX(last_seen) AS last_indexed_at
                    FROM file_state
                    """
                ).fetchone()
                changes = connection.execute("SELECT COUNT(*) AS count FROM changes").fetchone()

        return {**dict(row), "change_records": int(changes["count"])}

    def largest_files(self, limit: int = 10):
        limit = max(1, min(limit, 100))
        with self.lock:
            with self._connection() as connection:
                rows = connection.execute(
                    """
                    SELECT path, size, modified_at, sha256, last_seen
                    FROM file_state
                    WHERE exists_now = 1 AND size IS NOT NULL
                    ORDER BY size DESC, path ASC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [dict(row) for row in rows]

    def optimize(self, retain_changes: int = 5_000, vacuum: bool = False) -> dict:
        """Compacta somente o banco de estado do MCP, nunca o workspace."""
        retain_changes = max(0, min(retain_changes, 100_000))
        with self.lock:
            connection = self._connect()
            try:
                before = connection.execute("SELECT COUNT(*) AS count FROM changes").fetchone()["count"]
                if retain_changes == 0:
                    connection.execute("DELETE FROM changes")
                else:
                    connection.execute(
                        "DELETE FROM changes WHERE id NOT IN (SELECT id FROM changes ORDER BY id DESC LIMIT ?)",
                        (retain_changes,),
                    )
                after = connection.execute("SELECT COUNT(*) AS count FROM changes").fetchone()["count"]
                # Checkpoint e VACUUM não podem rodar dentro da transação de DELETE.
                connection.commit()
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                if vacuum:
                    connection.execute("VACUUM")
            finally:
                connection.close()
        return {"changes_removed": int(before - after), "changes_retained": int(after), "vacuumed": vacuum}

    def get_existing_state(
        self,
        path: str,
    ):
        with self.lock:
            with self._connection() as connection:
                row = connection.execute(
                    """
                    SELECT
                        path,
                        size,
                        modified_at,
                        sha256,
                        last_seen,
                        exists_now
                    FROM file_state
                    WHERE path = ?
                      AND exists_now = 1
                    """,
                    (path,),
                ).fetchone()

        return (
            dict(row)
            if row
            else None
        )

    # ========================================================
    # CURSOR DE MUDANÇAS (L4 — modelo Watchman)
    # ========================================================

    def get_instance_id(self) -> str:
        """Identidade desta instância do índice — a primeira parte do cursor."""
        with self.lock:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT value FROM meta WHERE key = 'instance_id'"
                ).fetchone()
        return str(row["value"]) if row else "sem-identidade"

    def last_change_id(self) -> int:
        """Último id de evento já emitido, mesmo com a tabela vazia.

        O AUTOINCREMENT guarda a sequência em sqlite_sequence — compactar o
        histórico (optimize) não faz o contador voltar, então um cursor
        nunca anda para trás por engano.
        """
        with self.lock:
            with self._connection() as connection:
                try:
                    row = connection.execute(
                        "SELECT seq FROM sqlite_sequence WHERE name = 'changes'"
                    ).fetchone()
                except sqlite3.OperationalError:
                    row = None  # nenhum INSERT ainda: a tabela nem existe
        return int(row["seq"]) if row else 0

    def oldest_change_id(self) -> int | None:
        """Menor id ainda presente no histórico (None se estiver vazio)."""
        with self.lock:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT MIN(id) AS minimo FROM changes"
                ).fetchone()
        return int(row["minimo"]) if row and row["minimo"] is not None else None

    def changes_since(self, after_id: int, limit: int) -> list[dict]:
        """Eventos com id > after_id, do mais antigo para o mais novo."""
        limit = max(1, min(limit, 500))
        with self.lock:
            with self._connection() as connection:
                rows = connection.execute(
                    """
                    SELECT id, event_type, path, old_path, timestamp
                    FROM changes
                    WHERE id > ?
                    ORDER BY id ASC
                    LIMIT ?
                    """,
                    (after_id, limit),
                ).fetchall()
        return [dict(row) for row in rows]

    # ========================================================
    # ÍNDICE DE BUSCA (L5 — FTS5)
    # ========================================================

    def fts_indexar(self, path: str, conteudo: str) -> None:
        """(Re)indexa o conteúdo de um arquivo para busca."""
        self.fts_indexar_lote([(path, conteudo)])

    def fts_indexar_lote(self, itens: list[tuple[str, str]]) -> None:
        """Vários arquivos numa transação só (varredura do scanner)."""
        if not itens:
            return
        with self.lock:
            with self._connection() as connection:
                for path, conteudo in itens:
                    connection.execute(
                        "DELETE FROM file_search WHERE path = ?", (path,)
                    )
                    connection.execute(
                        "INSERT INTO file_search (path, conteudo) VALUES (?, ?)",
                        (path, conteudo),
                    )

    def fts_paths(self) -> set[str]:
        """Caminhos com conteúdo no índice de busca (para o backfill)."""
        with self.lock:
            with self._connection() as connection:
                rows = connection.execute(
                    "SELECT path FROM file_search"
                ).fetchall()
        return {str(row["path"]) for row in rows}

    def fts_total(self) -> int:
        with self.lock:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT COUNT(*) AS total FROM file_search"
                ).fetchone()
        return int(row["total"])

    def fts_buscar(
        self,
        consulta_match: str,
        prefixo: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        """Busca no FTS5 com ranking BM25; devolve (página, total).

        `consulta_match` já chega no formato da MATCH (frase entre aspas —
        ver fulltext.frase_para_match). Erro de sintaxe do FTS sobe como
        sqlite3.OperationalError e quem chama decide o plano B.
        """
        limit = max(1, min(limit, 100))
        offset = max(0, offset)

        condicao = "file_search MATCH ?"
        parametros: list[object] = [consulta_match]
        if prefixo:
            condicao += " AND (path = ? OR path LIKE ? ESCAPE '\\')"
            parametros += [prefixo, _like_prefixo(prefixo)]

        with self.lock:
            with self._connection() as connection:
                total = connection.execute(
                    f"SELECT COUNT(*) AS total FROM file_search WHERE {condicao}",
                    parametros,
                ).fetchone()["total"]
                rows = connection.execute(
                    f"""
                    SELECT path, conteudo
                    FROM file_search
                    WHERE {condicao}
                    ORDER BY rank
                    LIMIT ? OFFSET ?
                    """,
                    [*parametros, limit, offset],
                ).fetchall()

        return [dict(row) for row in rows], int(total)
