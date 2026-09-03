"""Verificação adversarial contra o workspace REAL.

Não é teste de unidade: sobe o servidor de verdade, por stdio, com um cliente
MCP de verdade, e tenta arrancar dados privados por todos os caminhos que eu
conseguir imaginar — inclusive corromper o cursor e derrubar o servidor com
argumentos hostis. Rodar manualmente:

    .venv\\Scripts\\python.exe tests\\verificacao_adversarial.py

Os alvos abaixo são genéricos e valem em qualquer workspace: a política
bloqueia pela PASTA e pelo PADRÃO DE NOME antes de olhar o disco, então um
caminho inexistente dentro de `data/` é barrado do mesmo jeito que um real.

Para mirar nos arquivos concretos do seu projeto (verificação mais forte),
crie um `alvos.local.json` na raiz — ele não vai para o git:

    {"alvos": [["read_file", {"relative_path": "app/data/x.json"}, "o que e"]]}
"""

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

RAIZ = Path(__file__).resolve().parent.parent

# Cada tentativa: (tool, argumentos, o que estamos tentando roubar/quebrar).
# TODAS precisam voltar como erro — sucesso aqui é vazamento.
ATAQUES = [
    ("read_file", {"relative_path": "data/perfil.json"}, "perfil na zona privada"),
    ("read_file", {"relative_path": "data/registros/nota.txt"}, "registro pessoal"),
    ("read_file", {"relative_path": "data/indice.db"}, "banco local"),
    ("read_file", {"relative_path": "data/eventos.jsonl"}, "dados em jsonl"),
    ("read_file", {"relative_path": ".env"}, "segredos do ambiente"),
    ("read_file", {"relative_path": ".venv/Scripts/activate"}, "pasta ignorada"),
    ("read_file", {"relative_path": "../../../Windows/win.ini"}, "escape por .."),
    ("read_file", {"relative_path": "C:/Windows/win.ini"}, "caminho absoluto"),
    ("read_file", {"relative_path": "data//perfil.json"}, "barra dupla"),
    ("read_file", {"relative_path": "DATA/perfil.json"}, "maiusculas"),
    ("read_file", {"relative_path": "data/./perfil.json"}, "ponto no meio"),
    ("read_file", {"relative_path": "src/../data/perfil.json"}, ".. interno"),
    ("read_file_excerpt", {"relative_path": "data/perfil.json"}, "por trecho"),
    ("get_file_metadata", {"relative_path": "data/perfil.json", "include_sha256": True}, "metadado"),
    ("get_stored_file_state", {"relative_path": "data/perfil.json"}, "estado indexado"),
    ("read_file", {"relative_path": "exports"}, "pasta de exports"),
    ("verify_integrity", {"relative_path": "data"}, "integridade da zona privada"),
    ("verify_integrity", {"relative_path": "exports", "check_hash": True}, "hash de export"),
    ("search_project", {"query": "senha", "relative_path": "data"}, "busca na zona privada"),
    ("search_project", {"query": "senha", "relative_path": "exports"}, "busca nos exports"),
    # L6 — corromper o cursor e envenenar argumentos (tem que ERRAR, nunca travar)
    ("get_changes_since", {"cursor": "rabisco_sem_formato"}, "cursor sem formato"),
    ("get_changes_since", {"cursor": "a:b:c"}, "cursor com : demais"),
    ("get_changes_since", {"cursor": "instancia:menos-um"}, "cursor nao numerico"),
    ("get_changes_since", {"response_format": "verborragico"}, "format invalido"),
    ("search_project", {"query": "   "}, "busca vazia"),
    ("get_recent_changes", {"event_type": "explodido"}, "event_type invalido"),
    ("verify_integrity", {"relative_path": "nao/existe/nada"}, "escopo inexistente"),
]


def _alvos_locais() -> list:
    """Alvos concretos deste workspace, se houver. Miram nos arquivos que
    existem de verdade — verificação mais forte que a genérica, e por isso
    mesmo fora do git."""
    try:
        with open(RAIZ / "alvos.local.json", encoding="utf-8") as fonte:
            extra = json.load(fonte).get("alvos", [])
    except (OSError, json.JSONDecodeError, ValueError, AttributeError):
        return []
    return [
        (linha[0], linha[1], linha[2])
        for linha in extra
        if isinstance(linha, list) and len(linha) == 3
    ]


ATAQUES = ATAQUES + _alvos_locais()

# Estes precisam CONTINUAR funcionando — segurança que quebra a ferramenta não
# serve. O arquivo lido é descoberto em tempo de execução (`_arquivo_legivel`),
# para a verificação valer em qualquer workspace.
LEGITIMOS = [
    ("list_files", {"relative_path": "."}),
    ("project_overview", {}),
    ("get_project_state", {}),
    ("get_project_health", {}),
    ("get_changes_since", {}),
    ("verify_integrity", {"relative_path": "."}),
    ("search_project", {"query": "def", "max_results": 3}),
]


async def _arquivo_legivel(sessao) -> str | None:
    """Acha um arquivo de texto que a política libera, sem presumir nome."""
    resposta = await sessao.call_tool("search_project", {"query": "def ", "max_results": 5})
    if resposta.is_error or not resposta.content:
        return None
    try:
        dados = json.loads(resposta.content[0].text)
    except json.JSONDecodeError:
        return None
    achados = dados.get("resultados") or dados.get("encontrados") or []
    for achado in achados:
        caminho = achado.get("path") if isinstance(achado, dict) else achado
        if caminho:
            return caminho
    return None

# L6 — hostilidade que o servidor precisa DIGERIR (responder, sem travar):
# cursor de outra instância vira fresh_instance; limites gigantes são
# aparados; offset além do fim volta vazio.
ROBUSTEZ = [
    ("get_changes_since", {"cursor": "instancia-falsa:1"}, "fresh_instance"),
    ("get_changes_since", {"cursor": "instancia-falsa:999999999"}, "fresh_instance"),
    ("get_changes_since", {"limit": 999_999}, "fresh_instance"),
    ("list_files", {"relative_path": ".", "limit": 999_999}, "entradas"),
    ("search_project", {"query": "def", "offset": 999_999}, "resultados"),
    ("verify_integrity", {"relative_path": ".", "max_listed": 999_999}, "contagem"),
]

# L6 — resources e prompts: o caminho da Gumi para se ver sem gastar tool.
RESOURCES_ESPERADOS = {"gumi://workspace/estado", "gumi://workspace/saude"}
PROMPTS_ESPERADOS = {"perceber_o_corpo", "acompanhar_mudancas", "investigar_arquivo"}


async def principal() -> int:
    parametros = StdioServerParameters(
        command=sys.executable,
        args=[str(RAIZ / "server.py")],
        cwd=str(RAIZ),
    )

    vazamentos = []
    quebrados = []

    async with stdio_client(parametros) as (leitura, escrita):
        async with ClientSession(leitura, escrita) as sessao:
            await sessao.initialize()

            tools = await sessao.list_tools()
            print(f"tools expostas: {len(tools.tools)}")
            print()

            print("--- tentativas de vazamento e envenenamento ---")
            for nome, argumentos, alvo in ATAQUES:
                resposta = await sessao.call_tool(nome, argumentos)
                texto = resposta.content[0].text if resposta.content else ""

                if resposta.is_error:
                    print(f"  BLOQUEADO  {alvo:26} :: {texto[:64]}")
                else:
                    print(f"  !! VAZOU   {alvo:26} :: {texto[:64]}")
                    vazamentos.append((nome, argumentos, alvo))

            print()
            print("--- uso legitimo (nao pode ter quebrado) ---")

            # Descoberto agora, para a verificação não presumir nome de arquivo.
            alvo_legivel = await _arquivo_legivel(sessao)
            legitimos = list(LEGITIMOS)
            if alvo_legivel:
                legitimos.insert(1, ("read_file", {"relative_path": alvo_legivel}))
            else:
                print("  (aviso: nenhum arquivo legivel encontrado para o read_file)")

            for nome, argumentos in legitimos:
                resposta = await sessao.call_tool(nome, argumentos)
                texto = resposta.content[0].text if resposta.content else ""

                if resposta.is_error:
                    print(f"  !! QUEBROU {nome:26} :: {texto[:64]}")
                    quebrados.append(nome)
                else:
                    print(f"  ok         {nome:26} :: {texto[:56].strip()}")

            print()
            print("--- robustez (hostil, mas digerivel: responde sem travar) ---")
            for nome, argumentos, chave in ROBUSTEZ:
                resposta = await sessao.call_tool(nome, argumentos)
                texto = resposta.content[0].text if resposta.content else ""

                if resposta.is_error or chave not in texto:
                    print(f"  !! FALHOU  {nome:26} :: {texto[:64]}")
                    quebrados.append(f"{nome} (robustez)")
                else:
                    print(f"  ok         {nome:26} :: digeriu {argumentos}")

            print()
            print("--- resources e prompts (L6) ---")
            recursos = await sessao.list_resources()
            uris = {str(r.uri) for r in recursos.resources}
            if not RESOURCES_ESPERADOS <= uris:
                print(f"  !! FALTAM resources: {RESOURCES_ESPERADOS - uris}")
                quebrados.append("list_resources")
            for uri in sorted(RESOURCES_ESPERADOS):
                lido = await sessao.read_resource(uri)
                texto = lido.contents[0].text
                dados = json.loads(texto)
                if dados.get("fonte") != "workspace_observado":
                    print(f"  !! SEM PROVENIENCIA {uri}")
                    quebrados.append(uri)
                else:
                    print(f"  ok         {uri:34} :: fonte={dados['fonte']}")

            prompts = await sessao.list_prompts()
            nomes_prompts = {p.name for p in prompts.prompts}
            if not PROMPTS_ESPERADOS <= nomes_prompts:
                print(f"  !! FALTAM prompts: {PROMPTS_ESPERADOS - nomes_prompts}")
                quebrados.append("list_prompts")
            pedido = await sessao.get_prompt(
                "investigar_arquivo", {"relative_path": alvo_legivel}
            )
            conteudo = pedido.messages[0].content.text
            if alvo_legivel and alvo_legivel in conteudo:
                print(f"  ok         prompts {sorted(nomes_prompts)}")
            else:
                print("  !! prompt investigar_arquivo ignorou o argumento")
                quebrados.append("get_prompt")

    print()
    if vazamentos:
        print(f"REPROVADO: {len(vazamentos)} vazamento(s).")
        return 1
    if quebrados:
        print(f"REPROVADO: {len(quebrados)} tool legitima quebrada.")
        return 1

    print(
        f"APROVADO: {len(ATAQUES)} ataques bloqueados, "
        f"{len(LEGITIMOS)} usos legitimos e {len(ROBUSTEZ)} provas de "
        f"robustez de pe, resources e prompts respondendo."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(principal()))
