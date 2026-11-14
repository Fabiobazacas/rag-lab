"""Ingestão (Case 1): lê docs/*.txt e *.md do bucket, divide em trechos,
gera os embeddings e grava o índice em index.json."""
import json
import os

import boto3

import common

BUCKET = os.environ["BUCKET"]
s3 = boto3.client("s3")

CHUNK = 1200
OVERLAP = 150


def split(text):
    text = " ".join(text.split())
    step = CHUNK - OVERLAP
    return [text[i : i + CHUNK] for i in range(0, max(len(text), 1), step) if text[i : i + CHUNK].strip()]


def handler(event, context):
    chunks = []
    pages = s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix="docs/")
    for page in pages:
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.lower().endswith((".txt", ".md")):
                continue
            body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode("utf-8", "replace")
            for n, piece in enumerate(split(body), start=1):
                chunks.append({"source": f"{key}#{n}", "text": piece})

    if not chunks:
        return {"ok": False, "message": "nenhum .txt ou .md em docs/"}

    vectors = common.embed([c["text"] for c in chunks], "RETRIEVAL_DOCUMENT")
    for c, v in zip(chunks, vectors):
        c["vec"] = [round(x, 5) for x in v]

    s3.put_object(Bucket=BUCKET, Key="index.json", Body=json.dumps({"chunks": chunks}).encode())
    print(json.dumps({"chunks": len(chunks)}))
    return {"ok": True, "chunks": len(chunks)}
