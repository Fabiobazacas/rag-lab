"""Ingestão (Case 1): lê docs/*.txt, *.md e *.pdf do bucket, divide em trechos,
gera os embeddings e grava o índice em index.json.

PDF (inclusive escaneado): o Gemini transcreve o texto uma única vez e a transcrição
fica em cache/ no bucket. Uma nova ingestão reaproveita o cache e não gasta tokens."""
import json
import os

import boto3

import common

BUCKET = os.environ["BUCKET"]
s3 = boto3.client("s3")

CHUNK = 1200
OVERLAP = 150
MAX_PDF_BYTES = 10 * 1024 * 1024  # o Gemini aceita até 20 MB por requisição, e o base64 aumenta 33%
MAX_PDF_TOKENS = 8192             # teto da transcrição de cada PDF
MARGEM_MS = 15_000                # tempo reservado para embeddings e gravação do índice
MIN_CHAMADA_MS = 30_000           # abaixo disso, não começa outra transcrição


def split(text):
    text = " ".join(text.split())
    step = CHUNK - OVERLAP
    return [text[i : i + CHUNK] for i in range(0, max(len(text), 1), step) if text[i : i + CHUNK].strip()]


def _restante_ms(context):
    return context.get_remaining_time_in_millis() if context else 10**9


def _texto_do_pdf(key, obj, context, relatorio):
    """Devolve o texto do PDF (do cache ou do Gemini), ou None se não foi possível."""
    etag = obj.get("ETag", "").strip('"')
    cache_key = f"cache/{key}.{etag}.txt"
    try:
        return s3.get_object(Bucket=BUCKET, Key=cache_key)["Body"].read().decode("utf-8")
    except s3.exceptions.NoSuchKey:
        pass

    if obj.get("Size", 0) > MAX_PDF_BYTES:
        relatorio["ignorados"].append({"arquivo": key, "motivo": "maior que 10 MB"})
        return None
    disponivel = _restante_ms(context) - MARGEM_MS
    if disponivel < MIN_CHAMADA_MS:
        relatorio["pendentes"].append(key)
        return None

    data = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    try:
        texto, uso, fim = common.transcribe_pdf(data, timeout=min(90, disponivel // 1000), max_tokens=MAX_PDF_TOKENS)
    except common.GeminiError as e:
        motivo = "cota do Gemini esgotada" if e.status == 429 else f"erro do Gemini ({e.status})"
        relatorio["ignorados"].append({"arquivo": key, "motivo": motivo})
        return None

    relatorio["tokens_in"] += uso["tokens_in"]
    relatorio["tokens_out"] += uso["tokens_out"]
    if not texto.strip():
        relatorio["ignorados"].append({"arquivo": key, "motivo": "o Gemini não devolveu texto"})
        return None
    if fim == "MAX_TOKENS":
        # Texto cortado: usa o que veio, mas avisa. Não vai para o cache, para ser refeito depois.
        relatorio["truncados"].append(key)
        return texto
    s3.put_object(Bucket=BUCKET, Key=cache_key, Body=texto.encode("utf-8"))
    relatorio["transcritos"].append(key)
    return texto


def handler(event, context):
    chunks = []
    relatorio = {"transcritos": [], "truncados": [], "ignorados": [], "pendentes": [], "tokens_in": 0, "tokens_out": 0}
    pages = s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix="docs/")
    for page in pages:
        for obj in page.get("Contents", []):
            key = obj["Key"]
            nome = key.lower()
            if nome.endswith((".txt", ".md")):
                body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode("utf-8", "replace")
            elif nome.endswith(".pdf"):
                body = _texto_do_pdf(key, obj, context, relatorio)
                if body is None:
                    continue
            else:
                continue
            for n, piece in enumerate(split(body), start=1):
                chunks.append({"source": f"{key}#{n}", "text": piece})

    resumo = {k: v for k, v in relatorio.items() if v}
    if not chunks:
        return {"ok": False, "message": "nenhum .txt, .md ou .pdf utilizável em docs/", **resumo}

    vectors = common.embed([c["text"] for c in chunks], "RETRIEVAL_DOCUMENT")
    for c, v in zip(chunks, vectors):
        c["vec"] = [round(x, 5) for x in v]

    s3.put_object(Bucket=BUCKET, Key="index.json", Body=json.dumps({"chunks": chunks}).encode())
    print(json.dumps({"chunks": len(chunks), **resumo}))
    return {"ok": True, "chunks": len(chunks), **resumo}
