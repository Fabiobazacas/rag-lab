"""Agente (extensão): POST /agent {"question": "..."}.
O modelo decide quando buscar nos documentos (ferramenta buscar_documentos), pode buscar
mais de uma vez e responde quando tiver o que precisa. O laço tem limite de passos e de tempo."""
import json
import os
import time
import uuid

import boto3

import common

BUCKET = os.environ["BUCKET"]
TOP_K = int(os.environ["TOP_K"])
MAX_OUT = int(os.environ["MAX_OUTPUT_TOKEN"])
LOG_TABLE = os.environ["LOG_TABLE"]
MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "4"))  # buscas em sequência (rodadas de ferramenta)
MAX_CALLS_PER_STEP = 3  # buscas que o modelo pode pedir de uma vez
DEADLINE_S = 22  # depois disso, força a resposta final (o API Gateway corta em 30 s)

s3 = boto3.client("s3")
table = boto3.resource("dynamodb").Table(LOG_TABLE)
_index = None

SYSTEM = (
    "Você é um assistente que responde em português usando SOMENTE o que a ferramenta "
    "buscar_documentos retorna. Use a ferramenta para cada assunto distinto da pergunta, com "
    "consultas curtas e específicas; se o resultado for insuficiente, reformule e busque de novo. "
    "Cite a fonte entre colchetes, como [docs/arquivo.txt#1]. "
    "Se os trechos não trazem a resposta, diga que não encontrou na base."
)

TOOLS = [{
    "functionDeclarations": [{
        "name": "buscar_documentos",
        "description": "Busca nos documentos da base os trechos mais relevantes para uma consulta.",
        "parameters": {
            "type": "OBJECT",
            "properties": {"consulta": {"type": "STRING", "description": "o que procurar"}},
            "required": ["consulta"],
        },
    }]
}]


def load_index():
    global _index
    if _index is None:
        raw = s3.get_object(Bucket=BUCKET, Key="index.json")["Body"].read()
        _index = json.loads(raw)["chunks"]
    return _index


def search(query):
    qvec = common.embed([query], "RETRIEVAL_QUERY")[0]
    scored = sorted(
        ((sum(a * b for a, b in zip(qvec, c["vec"])), c) for c in load_index()),
        key=lambda x: x[0],
        reverse=True,
    )[:TOP_K]
    return [c for _, c in scored]


def reply(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False),
    }


def run_agent(question, started):
    """Laço do agente. Devolve (resposta, passos, uso)."""
    contents = [{"role": "user", "parts": [{"text": question}]}]
    steps = []
    total = {"tokens_in": 0, "tokens_out": 0}
    rounds = 0
    while True:
        # na última rodada (limite de passos ou de tempo) não oferece a ferramenta
        final = rounds >= MAX_STEPS or time.time() - started > DEADLINE_S
        timeout = max(5, min(25, 28 - (time.time() - started)))
        content, usage, _ = common.generate_step(
            SYSTEM, contents, None if final else TOOLS, MAX_OUT, timeout=timeout
        )
        total["tokens_in"] += usage["tokens_in"]
        total["tokens_out"] += usage["tokens_out"]
        calls = [p["functionCall"] for p in content["parts"] if "functionCall" in p]
        if final or not calls:
            text = "".join(p.get("text", "") for p in content["parts"]).strip()
            return text or "Não consegui concluir a resposta.", steps, total
        rounds += 1
        contents.append(content)
        results = []
        for call in calls[:MAX_CALLS_PER_STEP]:
            query = str((call.get("args") or {}).get("consulta") or "").strip()
            found = search(query) if query else []
            steps.append({"query": query, "sources": [c["source"] for c in found]})
            results.append({"functionResponse": {
                "name": call.get("name", "buscar_documentos"),
                "response": {"trechos": "\n\n".join(f"[{c['source']}]\n{c['text']}" for c in found)
                             or "nenhum trecho encontrado"},
            }})
        contents.append({"role": "user", "parts": results})


def handler(event, context):
    started = time.time()
    rid = str(uuid.uuid4())
    record = {"request_id": rid, "ts": int(started), "status": "ok", "mode": "agent"}
    try:
        question = (json.loads(event.get("body") or "{}").get("question") or "").strip()
        record["question"] = question
        if not question:
            return reply(400, {"error": "envie {\"question\": \"...\"}"})

        try:
            load_index()
        except s3.exceptions.NoSuchKey:
            return reply(409, {"error": "índice não existe: rode a função de ingestão"})

        answer, steps, usage = run_agent(question, started)
        sources = list(dict.fromkeys(s for st in steps for s in st["sources"]))
        record.update(usage)
        record["sources"] = sources
        record["searches"] = len(steps)
        return reply(200, {"answer": answer, "steps": steps, "sources": sources, **usage, "request_id": rid})

    except common.GeminiError as e:
        record["status"] = f"gemini_{e.status}"
        record["error"] = str(e)[:300]
        if e.status == 429:
            return reply(503, {"error": "cota do Gemini esgotada, tente mais tarde", "request_id": rid})
        return reply(502, {"error": "falha ao chamar o modelo", "request_id": rid})
    except Exception as e:  # registra e devolve erro genérico
        record["status"] = "erro"
        record["error"] = str(e)[:300]
        return reply(500, {"error": "erro interno", "request_id": rid})
    finally:
        record["latency_ms"] = int((time.time() - started) * 1000)
        try:
            table.put_item(Item=record)
        except Exception as e:
            print("falha ao gravar o registro:", e)
