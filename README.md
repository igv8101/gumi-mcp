# Gumi-MCP

**Servidor MCP local, somente leitura, que expõe um repositório a agentes de IA sem expor os dados que vivem dentro dele.**

[![testes](https://github.com/usuario-teste/gumi-mcp/actions/workflows/testes.yml/badge.svg)](https://github.com/usuario-teste/gumi-mcp/actions/workflows/testes.yml)
![python](https://img.shields.io/badge/python-3.12-blue)
![licença](https://img.shields.io/badge/licen%C3%A7a-MIT-green)

---

## O problema

Conectar um agente de IA ao seu repositório parece simples: aponte um servidor de
filesystem para a pasta e pronto. Só que um projeto pessoal raramente é só código.
Ao lado dele moram dados de uso, exports, mídia, bancos locais — e **leitura via MCP
sai da sua máquina**.

Este servidor nasceu de um projeto pessoal de IA, e a primeira medição foi
desconfortável:

> Dos **8.970** arquivos do workspace, **8.034 eram dados pessoais** e apenas **936
> eram código**. O servidor entregava os 8.970.

O Gumi-MCP existe para resolver isso: o agente enxerga o código, e só o código.

## Como resolve

**Uma política, três camadas, um caminho só.** Nenhuma tool manipula `Path` por conta
própria — todas passam por `security.py`:

```
resolver()            o caminho está dentro da raiz? (resolve symlinks e `..` antes)
    ↓
esta_ignorado()       é pasta gerada? (.git, .venv, node_modules…)
    ↓
motivo_privacidade()  é zona de dados? (pasta, ou padrão de nome: .env, *.db, *.jsonl…)
    ↓
extensao_legivel()    é formato de texto conhecido? (allowlist, não denylist)
```

Duas decisões que valem explicar:

**Indexar não é servir.** Um `.png` entra no índice porque o metadado é útil; o
conteúdo nunca é lido. Já um caminho dentro de uma pasta privada não entra nem como
metadado — o *nome* de um arquivo de dados já é um dado.

**O que é bloqueado continua aparecendo**, marcado como `bloqueado: "privado"`.
Esconder criaria um mapa mentiroso do repositório; o que não pode é o conteúdo sair.

## O que o agente ganha

| | |
|---|---|
| **Cursor sem perda** | `get_changes_since(cursor)` no modelo do [Watchman](https://facebook.github.io/watchman/docs/file-query.html): "o que mudou desde X", sem perder nem repetir evento. Cursor inválido devolve `fresh_instance` em vez de mentir. |
| **Verificação de integridade** | `verify_integrity()` no modelo de [FIM](https://documentation.wazuh.com/current/user-manual/capabilities/file-integrity/how-it-works.html) (AIDE/Tripwire): filesystem × baseline → `{iguais, modificados, sumidos, novos}`. |
| **Índice que não mente** | Watcher em tempo real **e** scanner de reconciliação periódico — o mesmo par que o Wazuh usa, porque watcher sozinho perde o que acontece enquanto o servidor está desligado. |
| **Busca com contexto** | FTS5 com BM25 devolvendo `{path, linha, trecho}` — não uma lista de caminhos para o agente reler. |
| **Erros que ensinam** | `ToolError` dizendo o que fazer a seguir, não `"Error executing tool X"`. |
| **Proveniência** | Todo JSON carrega `fonte: workspace_observado` — o consumidor sabe que aquilo é observação, não conclusão. |

17 tools, 2 resources, 3 prompts. Todas com `readOnlyHint` e `openWorldHint=false`.

## Verificação

```
116 testes  ·  48 ataques adversariais bloqueados  ·  7 usos legítimos intactos
```

Além dos testes de unidade, `tests/verificacao_adversarial.py` **sobe o servidor de
verdade contra um workspace de verdade** e tenta arrancar dados por todo caminho que
consegui imaginar: `..`, caminho absoluto, maiúsculas (`DATA/`), barra dupla, `.` no
meio, `..` interno, leitura por trecho, por metadado, pelo índice, e pela busca. Mais
sete formas de envenenar argumentos e seis de hostilidade que o servidor precisa
*digerir* sem travar (cursor de outra instância, limites de 999.999).

Segurança que quebra a ferramenta não serve — por isso a suíte também exige que o uso
legítimo continue de pé.

## Instalação

```bash
git clone https://github.com/usuario-teste/gumi-mcp
cd gumi-mcp
python -m venv .venv && .venv/Scripts/activate      # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
```

Defina a raiz a servir. **Sem ela o servidor serve apenas a própria pasta** — de
propósito: melhor servir de menos do que servir demais em silêncio.

```bash
GUMI_MCP_ROOT=/caminho/do/seu/projeto
```

### Conectar a um cliente

`scripts/conectar_clientes.py` registra o servidor no Claude Desktop, no Codex
(app e CLI) e no VS Code de uma vez — com backup datado, revalidação e modo
`--conferir`. Ou configure na mão, com o mesmo par comando+args:

```json
{
  "mcpServers": {
    "gumi-workspace": {
      "command": "/caminho/gumi-mcp/.venv/Scripts/python.exe",
      "args": ["/caminho/gumi-mcp/server.py"],
      "env": { "GUMI_MCP_ROOT": "/caminho/do/seu/projeto" }
    }
  }
}
```

> **ChatGPT** não entra nessa lista: ele conecta apenas a servidores MCP remotos, com
> URL pública. Publicar este servidor na internet anularia o motivo dele existir.

### Ajustar a política ao seu projeto

Os padrões são conservadores. Para acrescentar as pastas do seu projeto sem
versioná-las, crie um `privado.local.json` (já ignorado pelo git):

```json
{ "diretorios": ["minha_pasta_de_dados"], "prefixos": ["_rascunho"] }
```

Ele **soma** aos padrões de `config.py` — nunca afrouxa.

## Configuração

| Variável | Padrão | O que faz |
|---|---|---|
| `GUMI_MCP_ROOT` | a própria pasta | raiz servida |
| `GUMI_MCP_DATABASE` | `data/gumi_state.db` | índice SQLite |
| `GUMI_MCP_LOGS` | `logs/` | log e trilha de auditoria |
| `GUMI_MCP_SCAN_HOURS` | `6` | intervalo do scanner de reconciliação |

O servidor escreve **apenas** no próprio banco e nos próprios logs. Nada no
workspace é criado, alterado, movido ou executado — nunca.

## Notas de implementação

Três coisas que só apareceram construindo, e que valem para qualquer servidor MCP:

**Em stdio, `stdout` pertence ao protocolo.** Um `print()` de debug derruba a conexão
JSON-RPC. Todo log aqui vai para arquivo ou `stderr`.

**Só `ToolError` chega ao cliente.** Qualquer outra exceção vira
`"Error executing tool X"` genérico, e o modelo fica sem saber se errou o caminho, se
o arquivo é grande demais ou se foi bloqueado.

**Watcher em tempo real não basta.** Se o servidor sobe e desce a cada consulta, ele
não vê nada nos intervalos — e o scanner da subida grava *depois* que a primeira
resposta foi montada, o que faz a percepção chegar com uma consulta de atraso. A
correção é perguntar duas vezes na mesma sessão, usando o cursor da primeira.

## Licença

MIT — veja [LICENSE](LICENSE).
