"""
Agente nível 1 — um loop de "pensar → chamar ferramenta → observar" sem framework.

Só usa a biblioteca padrão do Python (nada de pip install).
Fala com qualquer API compatível com o formato da OpenAI (Groq, Gemini, Ollama...).

Uso:
    export GROQ_API_KEY="sua-chave"        # https://console.groq.com/keys
    python agente.py

Trocar de provedor:
    PROVEDOR=gemini GEMINI_API_KEY="..." python agente.py
    PROVEDOR=ollama python agente.py       # local, precisa do Ollama rodando
    MODELO=outro-modelo python agente.py   # sobrescreve o modelo padrão
"""

import ast
import json
import operator
import os
import urllib.parse
import urllib.request
from datetime import datetime

# ---------------------------------------------------------------------------
# 1. Configuração do provedor (todos falam o mesmo formato /chat/completions)
# ---------------------------------------------------------------------------

PROVEDORES = {
    "groq": {
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "chave_env": "GROQ_API_KEY",
        "modelo": "openai/gpt-oss-120b",
    },
    "gemini": {
        "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "chave_env": "GEMINI_API_KEY",
        "modelo": "gemini-flash-latest",
    },
    "ollama": {
        "url": "http://localhost:11434/v1/chat/completions",
        "chave_env": None,  # local, sem chave
        "modelo": "qwen3",
    },
}

PROVEDOR = os.environ.get("PROVEDOR", "groq")
CONFIG = PROVEDORES[PROVEDOR]
MODELO = os.environ.get("MODELO", CONFIG["modelo"])
MAX_PASSOS = 8  # trava de segurança contra loop infinito


# ---------------------------------------------------------------------------
# 2. Ferramentas: funções Python comuns que o modelo pode pedir para executar
# ---------------------------------------------------------------------------

_OPERADORES = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv, ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def calcular(expressao: str) -> str:
    """Avalia uma expressão aritmética com segurança (sem usar eval)."""
    def avaliar(no):
        if isinstance(no, ast.Constant) and isinstance(no.value, (int, float)):
            return no.value
        if isinstance(no, ast.BinOp) and type(no.op) in _OPERADORES:
            return _OPERADORES[type(no.op)](avaliar(no.left), avaliar(no.right))
        if isinstance(no, ast.UnaryOp) and type(no.op) in _OPERADORES:
            return _OPERADORES[type(no.op)](avaliar(no.operand))
        raise ValueError("expressão não suportada")

    return str(avaliar(ast.parse(expressao, mode="eval").body))


def data_hora_atual() -> str:
    """Devolve a data e a hora locais."""
    return datetime.now().strftime("%A, %d/%m/%Y %H:%M")


def buscar_wikipedia(termo: str) -> str:
    """Procura o termo na Wikipédia em português e devolve o resumo do artigo."""
    cabecalho = {"User-Agent": "agente-nivel-1/1.0 (projeto de estudo)"}

    busca = "https://pt.wikipedia.org/w/api.php?" + urllib.parse.urlencode(
        {"action": "opensearch", "search": termo, "limit": 1, "format": "json"}
    )
    with urllib.request.urlopen(urllib.request.Request(busca, headers=cabecalho), timeout=15) as r:
        titulos = json.load(r)[1]
    if not titulos:
        return "Nenhum artigo encontrado."

    resumo = "https://pt.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(titulos[0])
    with urllib.request.urlopen(urllib.request.Request(resumo, headers=cabecalho), timeout=15) as r:
        dados = json.load(r)
    return f"{dados.get('title')}: {dados.get('extract', '(sem resumo)')}"


# Mapa nome -> função, usado para executar o que o modelo pedir
FERRAMENTAS = {
    "calcular": calcular,
    "data_hora_atual": data_hora_atual,
    "buscar_wikipedia": buscar_wikipedia,
}

# Descrição das ferramentas no formato que o modelo entende (JSON Schema)
ESQUEMA_FERRAMENTAS = [
    {
        "type": "function",
        "function": {
            "name": "calcular",
            "description": "Calcula uma expressão aritmética. Use sempre que precisar fazer contas.",
            "parameters": {
                "type": "object",
                "properties": {"expressao": {"type": "string", "description": "Ex.: (12.5 * 4) / 3"}},
                "required": ["expressao"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "data_hora_atual",
            "description": "Retorna a data e a hora atuais.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "buscar_wikipedia",
            "description": "Busca um assunto na Wikipédia em português e retorna um resumo.",
            "parameters": {
                "type": "object",
                "properties": {"termo": {"type": "string", "description": "Assunto a pesquisar"}},
                "required": ["termo"],
            },
        },
    },
]

PROMPT_SISTEMA = (
    "Você é um assistente prestativo que responde em português. "
    "Use as ferramentas disponíveis quando precisar de contas, da data atual "
    "ou de informações factuais. Não invente resultados de ferramentas."
)


# ---------------------------------------------------------------------------
# 3. Chamada ao modelo (um POST HTTP comum)
# ---------------------------------------------------------------------------

def chamar_modelo(mensagens: list) -> dict:
    corpo = {"model": MODELO, "messages": mensagens, "tools": ESQUEMA_FERRAMENTAS}
    cabecalho = {"Content-Type": "application/json"}
    if CONFIG["chave_env"]:
        chave = os.environ.get(CONFIG["chave_env"])
        if not chave:
            raise SystemExit(f"Defina a variável de ambiente {CONFIG['chave_env']}.")
        cabecalho["Authorization"] = f"Bearer {chave}"

    req = urllib.request.Request(
        CONFIG["url"], data=json.dumps(corpo).encode(), headers=cabecalho, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)["choices"][0]["message"]
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Erro {e.code} da API: {e.read().decode()[:500]}") from None


def executar_ferramenta(chamada: dict) -> str:
    nome = chamada["function"]["name"]
    try:
        argumentos = json.loads(chamada["function"].get("arguments") or "{}")
        return str(FERRAMENTAS[nome](**argumentos))
    except Exception as e:  # o erro volta para o modelo, que pode tentar de novo
        return f"ERRO ao executar {nome}: {e}"


# ---------------------------------------------------------------------------
# 4. O loop do agente — o coração de tudo
# ---------------------------------------------------------------------------

def rodar_agente(mensagens: list) -> str:
    for _ in range(MAX_PASSOS):
        resposta = chamar_modelo(mensagens)
        mensagens.append(resposta)

        chamadas = resposta.get("tool_calls") or []
        if not chamadas:  # sem ferramentas pedidas = resposta final
            return resposta.get("content") or ""

        for chamada in chamadas:
            resultado = executar_ferramenta(chamada)
            print(f"  🔧 {chamada['function']['name']}({chamada['function'].get('arguments')}) → {resultado[:120]}")
            mensagens.append({"role": "tool", "tool_call_id": chamada["id"], "content": resultado})

    return "Parei: atingi o limite de passos sem chegar a uma resposta."


def main():
    print(f"Agente pronto ({PROVEDOR} / {MODELO}). Digite 'sair' para encerrar.\n")
    mensagens = [{"role": "system", "content": PROMPT_SISTEMA}]
    while True:
        try:
            pergunta = input("Você: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if pergunta.lower() in {"sair", "exit", "quit"}:
            break
        if not pergunta:
            continue
        mensagens.append({"role": "user", "content": pergunta})
        try:
            print(f"Agente: {rodar_agente(mensagens)}\n")
        except RuntimeError as e:
            print(f"⚠️  {e}\n")


if __name__ == "__main__":
    main()