"""Consulta (Case 2): POST /ask {"question": "..."}.
Busca os trechos mais próximos, pergunta ao Gemini e registra cada consulta."""
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

s3 = boto3.client("s3")
table = boto3.resource("dynamodb").Table(LOG_TABLE)
_index = None

SYSTEM = (
    "Você responde em português usando SOMENTE os trechos fornecidos. "
    "Cite a fonte entre colchetes, como [docs/arquivo.txt#1]. "
    "Se os trechos não trazem a resposta, diga que não encontrou na base."
)


def load_index():
    global _index
    if _index is None:
        raw = s3.get_object(Bucket=BUCKET, Key="index.json")["Body"].read()
        _index = json.loads(raw)["chunks"]
    return _index


def reply(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False),
    }


def handler(event, context):
    started = time.time()
    rid = str(uuid.uuid4())
    record = {"request_id": rid, "ts": int(started), "status": "ok"}
    try:
        question = (json.loads(event.get("body") or "{}").get("question") or "").strip()
        record["question"] = question
        if not question:
            return reply(400, {"error": "envie {\"question\": \"...\"}"})

        try:
            chunks = load_index()
        except s3.exceptions.NoSuchKey:
            return reply(409, {"error": "índice não existe: rode a função de ingestão"})

        qvec = common.embed([question], "RETRIEVAL_QUERY")[0]
        scored = sorted(
            ((sum(a * b for a, b in zip(qvec, c["vec"])), c) for c in chunks),
            key=lambda x: x[0],
            reverse=True,
        )[:TOP_K]
        sources = [c["source"] for _, c in scored]
        record["sources"] = sources

        context_text = "\n\n".join(f"[{c['source']}]\n{c['text']}" for _, c in scored)
        prompt = f"Trechos:\n{context_text}\n\nPergunta: {question}"
        answer, usage = common.generate(SYSTEM, prompt, MAX_OUT)
        record.update(usage)
        return reply(200, {"answer": answer, "sources": sources, **usage, "request_id": rid})

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
