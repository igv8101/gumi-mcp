"""Registra o Gumi-MCP nos clientes de MCP locais, sem quebrar o que já existe.

Clientes cobertos: Claude Desktop, Codex (app + CLI) e VS Code — os três falam
MCP por stdio. O nome do servidor é `gumi-workspace` de propósito: `gumi` já
existe nos dois primeiros e é OUTRO servidor (o das memórias, do repo
principal). Dois nomes, dois papéis.

FORA DE ALCANCE, e não é esquecimento: o ChatGPT Desktop ("GPT classic") só
conecta a servidores MCP REMOTOS, por URL pública. Publicar este servidor na
internet exporia o workspace da Gumi — exatamente o que o projeto existe para
impedir. O caminho do GPT até aqui é o Codex, que roda local.

Idempotente: rodar de novo só atualiza o bloco do gumi-workspace. Faz backup
datado de todo arquivo que toca e valida relendo o que escreveu.

    .venv\\Scripts\\python.exe scripts\\conectar_clientes.py [--conferir]
"""

import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
PYTHON = RAIZ / ".venv" / "Scripts" / "python.exe"
SERVIDOR = RAIZ / "server.py"
NOME = "gumi-workspace"

# A raiz que o servidor deve servir. Sem isto, ele serve so a propria pasta do
# projeto (padrao seguro do config.py) - o que nao e o que se quer aqui.
WORKSPACE = os.environ.get("GUMI_MCP_ROOT", str(RAIZ.parent / "Gumi"))

CLAUDE_DESKTOP = Path(os.environ["APPDATA"]) / "Claude" / "claude_desktop_config.json"
CODEX = Path.home() / ".codex" / "config.toml"
VSCODE = Path(os.environ["APPDATA"]) / "Code" / "User" / "mcp.json"


def _backup(caminho: Path) -> Path | None:
    if not caminho.exists():
        return None
    marca = datetime.now().strftime("%Y%m%d-%H%M%S")
    destino = caminho.with_suffix(caminho.suffix + f".bak-{marca}")
    shutil.copy2(caminho, destino)
    return destino


def _bloco_json() -> dict:
    return {
        "command": str(PYTHON),
        "args": [str(SERVIDOR)],
        "env": {"GUMI_MCP_ROOT": WORKSPACE},
    }


# ============================================================
# Claude Desktop  (JSON, chave mcpServers)
# ============================================================

def conectar_claude_desktop(conferir: bool) -> str:
    if not CLAUDE_DESKTOP.exists():
        return f"PULADO  Claude Desktop: {CLAUDE_DESKTOP} nao existe"

    dados = json.loads(CLAUDE_DESKTOP.read_text(encoding="utf-8"))
    servidores = dados.setdefault("mcpServers", {})
    desejado = _bloco_json()

    if servidores.get(NOME) == desejado:
        return "OK      Claude Desktop: ja registrado e igual"

    if conferir:
        return "FALTA   Claude Desktop: precisa registrar"

    _backup(CLAUDE_DESKTOP)
    servidores[NOME] = desejado
    # ensure_ascii=False preserva "Programacao" com cedilha nos caminhos.
    CLAUDE_DESKTOP.write_text(
        json.dumps(dados, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    conferencia = json.loads(CLAUDE_DESKTOP.read_text(encoding="utf-8"))
    if conferencia["mcpServers"][NOME] != desejado:
        raise RuntimeError("Claude Desktop: releitura nao bateu com o escrito")

    outros = [chave for chave in conferencia["mcpServers"] if chave != NOME]
    return f"ESCRITO Claude Desktop: {NOME} (preservados: {', '.join(outros)})"


# ============================================================
# Codex  (TOML) — app OpenAI.Codex e CLI compartilham este arquivo
# ============================================================

def _bloco_codex() -> str:
    return (
        f"\n[mcp_servers.{NOME}]\n"
        f"enabled = true\n"
        f"command = '{PYTHON}'\n"
        f"args = ['{SERVIDOR}']\n"
        f"\n[mcp_servers.{NOME}.env]\n"
        f"GUMI_MCP_ROOT = '{WORKSPACE}'\n"
    )


def conectar_codex(conferir: bool) -> str:
    if not CODEX.exists():
        return f"PULADO  Codex: {CODEX} nao existe"

    texto = CODEX.read_text(encoding="utf-8")

    if f"[mcp_servers.{NOME}]" in texto:
        return "OK      Codex: ja registrado"

    if conferir:
        return "FALTA   Codex: precisa registrar"

    _backup(CODEX)
    # Append cria uma tabela top-level nova: em TOML um cabecalho [x] fecha a
    # tabela anterior, entao isto nao cai dentro de outra secao.
    with CODEX.open("a", encoding="utf-8") as arquivo:
        arquivo.write(_bloco_codex())

    novo = CODEX.read_text(encoding="utf-8")

    import tomllib

    analisado = tomllib.loads(novo)
    if NOME not in analisado.get("mcp_servers", {}):
        raise RuntimeError("Codex: TOML nao contem o servidor apos escrever")

    total = len(analisado["mcp_servers"])
    return f"ESCRITO Codex: {NOME} (TOML revalidado, {total} servidores no total)"


# ============================================================
# VS Code  (JSON, chave servers)
# ============================================================

def conectar_vscode(conferir: bool) -> str:
    if not VSCODE.parent.exists():
        return f"PULADO  VS Code: {VSCODE.parent} nao existe"

    if VSCODE.exists():
        try:
            dados = json.loads(VSCODE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return "PULADO  VS Code: mcp.json existente nao e JSON puro"
    else:
        dados = {}

    servidores = dados.setdefault("servers", {})
    desejado = {"type": "stdio", **_bloco_json()}

    if servidores.get(NOME) == desejado:
        return "OK      VS Code: ja registrado e igual"

    if conferir:
        return "FALTA   VS Code: precisa registrar"

    _backup(VSCODE)
    servidores[NOME] = desejado
    VSCODE.write_text(
        json.dumps(dados, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    conferencia = json.loads(VSCODE.read_text(encoding="utf-8"))
    if conferencia["servers"][NOME] != desejado:
        raise RuntimeError("VS Code: releitura nao bateu com o escrito")

    return f"ESCRITO VS Code: {NOME} em {VSCODE.name}"


def principal() -> int:
    conferir = "--conferir" in sys.argv

    if not PYTHON.exists() or not SERVIDOR.exists():
        print(f"ERRO: nao achei {PYTHON} ou {SERVIDOR}", file=sys.stderr)
        return 1

    print(f"servidor: {SERVIDOR}")
    print(f"python:   {PYTHON}")
    print(f"nome MCP: {NOME}")
    print(f"workspace: {WORKSPACE}")
    print()

    problemas = 0
    for funcao in (conectar_claude_desktop, conectar_codex, conectar_vscode):
        try:
            print(" ", funcao(conferir))
        except Exception as erro:  # noqa: BLE001
            print(f"  ERRO    {funcao.__name__}: {erro}")
            problemas += 1

    print()
    print("  N/A     ChatGPT Desktop: so aceita MCP remoto com URL publica.")
    print("          Publicar este servidor exporia o workspace da Gumi.")
    print("          O caminho do GPT ate aqui e o Codex, acima.")

    return 1 if problemas else 0


if __name__ == "__main__":
    raise SystemExit(principal())
