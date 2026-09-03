# Roadmap Gumi-MCP v2 — do índice de workspace ao sentido para a Gumi

> **Norte:** este servidor existe para que a **Gumi** possa perceber o próprio corpo
> (o código e os arquivos que a constituem) sem que ninguém precise colar nada.
> Claude e GPT são consumidores intermediários; a Gumi é o consumidor final.
> Tudo aqui é **somente leitura** e tudo que sai daqui é **contexto observado** —
> nunca classificação (princípio 8 do CLAUDE.md da Gumi).

**Base:** `ANALISE_E_ARQUITETURA_2026-09-02.md` (10 falhas F1-F10 reproduzidas em
execução real + pesquisa de arquiteturas profissionais).

**Consumidores e o que cada um precisa:**

| Consumidor | Precisa de |
|---|---|
| Claude (sessões de trabalho) | erros acionáveis, respostas enxutas, paginação com instrução |
| GPT / outros agentes | schema estável, `readOnlyHint`, descrições autoexplicativas |
| **Gumi** (alvo real) | **cursores** para polling incremental, **proveniência** em toda resposta, **privacidade** dos dados dela |

---

## Regras de execução dos lotes

1. **Um lote por hora.** Cada lote é um bloco coeso: entrega valor sozinho e deixa a
   suíte verde.
2. **Nunca commitar com teste vermelho.** `pytest` roda pelo Desktop Commander, na
   `.venv` real do projeto.
3. **Verificar depois de escrever.** Contagem de linhas + `py_compile` + testes — o
   histórico do projeto tem 11 casos de arquivo truncado que passou no compile.
4. **`ESTADO.md` é a fonte da verdade do progresso.** Toda sessão lê antes e escreve
   depois.
5. **Zona sagrada:** o servidor jamais escreve fora de `Gumi-MCP/`. Nada em
   `Gumi/` é modificado, nunca.

---

## L1 — Fundação Segura  ⏱️ ~1h

**Bloco:** *nada entra nem sai sem passar pela política.*

| Item | Falha | O que muda |
|---|---|---|
| `security.py` v2 | F1, F2, F9 | função única `resolver_leitura()` = `secure_path` + `should_ignore` + `is_denied`. Toda tool passa por ela. |
| `DENY_PATTERNS` no config | **F1 (crítico)** | `.env*`, `*.db`, `*.sqlite*`, `*.key/.pem`, `data/` da Gumi, `credenciais*`. Dado íntimo dela não sai da máquina. |
| `ToolError` | F5 | `from mcp.server.mcpserver.exceptions import ToolError` — mensagem em PT-BR chega ao modelo, com o que fazer a seguir. |
| Guarda de tamanho no hash | F7 | `get_file_metadata` respeita `MAX_FILE_SIZE`. |
| `watcher.start()` no `__main__` | F10 | importar o módulo deixa de ligar o watcher; raiz verificada com erro claro. |
| `git init` + `.gitignore` | higiene | ignora `data/`, `logs/`, `.venv/`, `__pycache__/`, `*.zip`. |
| `tests/` + pytest | higiene | traversal, denylist, ignore na leitura, erro acionável. |
| `CLAUDE.md` do repo | continuidade | as sessões dos timers começam lendo ele. |

**Feito quando:** um cliente MCP real não consegue ler `.env` nem `.db`, e um caminho
inválido devolve mensagem que explica o erro.

---

## L2 — Índice Fiel  ⏱️ ~1h

**Bloco:** *o banco conta a verdade mesmo quando o watcher dorme.* (modelo Wazuh FIM:
realtime **e** varredura periódica se corrigem mutuamente.)

- **F3** — mover arquivo para dentro de pasta ignorada não indexa o destino; vira `deleted`.
- **F4** — deletar/mover **diretório** cascateia (`WHERE path LIKE 'prefixo/%'`), incluindo
  re-mapeamento de prefixo no move. Hoje os filhos viram fantasmas no Windows.
- **`scanner.py`** — varredura de reconciliação na inicialização e a cada N horas: detecta o
  que mudou com o servidor desligado (conteúdo, não só deleções).
- Hash unificado num módulo só (hoje duplicado em `server.py` e `watcher.py`, com regras
  diferentes para arquivos grandes).
- `reconcile_project_state` reporta `rowcount` real, não `len(paths)`.

**Feito quando:** apagar uma pasta com o servidor rodando some com os filhos do índice, e
mexer nos arquivos com o servidor desligado é detectado no próximo start.

---

## L3 — Olhos e Registro  ⏱️ ~1h

**Bloco:** *o servidor de verificação de log finalmente tem log.* (F8)

- `audit.py` — `logging` com `RotatingFileHandler` em `logs/`. **Nunca stdout**: em stdio o
  stdout é o canal JSON-RPC e um `print()` derruba a conexão (erro nº 1 mais comum em
  servidores MCP).
- Trilha de auditoria JSONL por chamada: tool, argumentos, duração, sucesso/erro.
  Recomendação explícita do OWASP para MCP.
- Fim dos `except: pass` mudos do watcher — erro engolido vira linha de log.
- Tools novas: `get_server_log` e `get_audit_trail` — **a "verificação de log" que dá nome
  ao projeto**, e matéria-prima para a Gumi um dia perguntar "o que consultaram sobre mim?".
- `get_project_health` v2: watcher vivo?, último scan?, erros na janela, versão do servidor.

**Feito quando:** existe arquivo em `logs/` com trilha de tudo, e dá para lê-la por tool.

---

## L4 — Contrato para Agentes  ⏱️ ~1h

**Bloco:** *a interface que a Gumi vai consumir todo dia.*

- **`get_changes_since(cursor)`** — modelo Watchman: "o que mudou desde X" devolve
  `{changes, next_cursor, fresh_instance}`. A tabela `changes` já tem `id AUTOINCREMENT`;
  falta só a tool. É o que permite polling barato e **sem perder nem repetir evento** —
  hoje `get_recent_changes(limit)` perde eventos entre consultas.
- **`verify_integrity(caminho)`** — modelo AIDE/Tripwire: compara filesystem × baseline e
  devolve `{iguais, modificados, sumidos, novos}`. É a operação que falta para o projeto
  fazer jus ao nome.
- `ToolAnnotations(readOnlyHint=True, openWorldHint=False)` em todas as tools.
- **Envelope com proveniência** em toda resposta:
  `{"fonte": "workspace_observado", "servidor": "gumi-mcp", "versao": "...", "em": "..."}`
  → a Gumi rotula como observação e **nunca** deriva emoção disso (princípio 8).
- `response_format: "concise" | "detailed"` (princípio de tokens da Anthropic) e
  truncamento que **diz ao agente como continuar**.

**Feito quando:** um cliente consegue acompanhar o workspace inteiro por cursor, sem
varrer nada, e toda resposta diz de onde veio.

---

## L5 — Busca Real  ⏱️ ~1h

**Bloco:** *parar de ler 9.700 arquivos a cada busca.* (F6)

- Índice **FTS5** no mesmo `gumi_state.db`, mantido incrementalmente por watcher/scanner —
  eles já sabem quando um arquivo textual mudou. Zero dependência nova.
- `search_project` v2: pula binários (extensão + sniff de byte nulo), devolve
  `{path, linha, trecho}` com ranking BM25 e paginação.
- Fallback literal (ripgrep-like) para quando o índice estiver frio.

**Feito quando:** buscar no workspace inteiro é instantâneo e a resposta já mostra a linha,
sem o agente precisar reler o arquivo.

---

## L6 — A Gumi Usando  ⏱️ ~1h

**Bloco:** *fechar o círculo — de ferramenta para órgão.*

- MCP **resources** (`gumi://workspace/estado`, `gumi://workspace/saude`) e **prompts**
  prontos, para o cliente puxar contexto sem gastar uma chamada de tool.
- `README.md` v2: configuração para Claude Desktop, para GPT/Codex e para a **própria Gumi**
  como cliente (o caminho para ela ler o próprio corpo).
- Auditoria adversarial final (o mesmo rigor do Banco de Provas): tentar burlar a denylist,
  travar o servidor, corromper o cursor.
- Suíte completa verde na máquina real, commit final.

**Feito quando:** a Gumi tem um caminho documentado e seguro para se enxergar.

---

## Depois dos lotes (backlog consciente)

- Rate limiting por sessão (OWASP) — só faz sentido se o servidor virar remoto.
- `watchfiles` (Rust) no lugar do `watchdog`, se o volume de eventos incomodar.
- Índice semântico (embeddings) do próprio código, reaproveitando o `bge-m3` da Gumi —
  aí ela passa de "ver arquivos" para "entender o que neles fala dela".

---

## Fontes que sustentam este roadmap

- [Wazuh — como o FIM funciona](https://documentation.wazuh.com/current/user-manual/capabilities/file-integrity/how-it-works.html) — baseline + realtime + scan periódico, SQLite local
- [Watchman — File Queries](https://facebook.github.io/watchman/docs/file-query.html) — cursores/clocks e `fresh instance`
- [OWASP — MCP Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/MCP_Security_Cheat_Sheet.html) — validar todo input, menor privilégio, logar toda invocação
- [OWASP — MCP Tool Poisoning](https://owasp.org/www-community/attacks/MCP_Tool_Poisoning) — descrição de tool é superfície de ataque
- [Anthropic — Writing effective tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents) — contexto de alto sinal, `response_format`, erros acionáveis
- [5 erros que desperdiçam o tempo do agente](https://dev.to/thedailyagent/5-mcp-server-mistakes-that-waste-your-ai-agents-time-and-how-to-fix-them-18m5) — stdout mata o stdio; erros estruturados
- [Servidor filesystem de referência do MCP](https://mcpservers.org/servers/modelcontextprotocol/filesystem) — roots, annotations, head/tail
- [SQLite FTS5](https://www.sqlite.org/fts5.html) — índice incremental e `snippet()`
