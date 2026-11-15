"""Funções comuns: chamadas à API do Gemini (sem dependências externas)."""
import base64
import json
import math
import os
import urllib.error
import urllib.request

API = "https://generativelanguage.googleapis.com/v1beta/"
KEY = os.environ["GEMINI_API_KEY"]
GEN_MODEL = os.environ["GEMINI_MODEL"]
EMBED_MODEL = os.environ["EMBED_MODEL"]
DIMS = 768


class GeminiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _post(path, body, timeout=25):
    req = urllib.request.Request(
        API + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": KEY},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise GeminiError(e.code, e.read().decode(errors="replace")[:300])
    except urllib.error.URLError as e:
        raise GeminiError(0, f"sem acesso à API do Gemini: {e.reason}")


def _normalize(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def embed(texts, task):
    """Devolve um vetor normalizado por texto. task: RETRIEVAL_DOCUMENT ou RETRIEVAL_QUERY."""
    out = []
    for i in range(0, len(texts), 50):
        batch = texts[i : i + 50]
        body = {
            "requests": [
                {
                    "model": f"models/{EMBED_MODEL}",
                    "content": {"parts": [{"text": t}]},
                    "taskType": task,
                    "outputDimensionality": DIMS,
                }
                for t in batch
            ]
        }
        data = _post(f"models/{EMBED_MODEL}:batchEmbedContents", body)
        out += [_normalize(e["values"]) for e in data["embeddings"]]
    return out


PDF_PROMPT = (
    "Transcreva fielmente o texto deste documento, na ordem das paginas, sem resumir, "
    "sem comentar e sem corrigir. Inclua o texto que aparece como imagem (documento escaneado). "
    "Responda somente com a transcricao."
)


def transcribe_pdf(data, timeout, max_tokens=8192):
    """Transcreve um PDF (inclusive escaneado). Devolve (texto, uso, finishReason)."""
    body = {
        "contents": [{"parts": [
            {"inline_data": {"mime_type": "application/pdf", "data": base64.b64encode(data).decode()}},
            {"text": PDF_PROMPT},
        ]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0},
    }
    resp = _post(f"models/{GEN_MODEL}:generateContent", body, timeout=timeout)
    cand = (resp.get("candidates") or [{}])[0]
    parts = (cand.get("content") or {}).get("parts") or []
    usage = resp.get("usageMetadata", {})
    return (
        "".join(p.get("text", "") for p in parts),
        {"tokens_in": usage.get("promptTokenCount", 0), "tokens_out": usage.get("candidatesTokenCount", 0)},
        cand.get("finishReason", ""),
    )


def generate_step(system, contents, tools, max_tokens, timeout=25):
    """Um passo do agente: envia a conversa (e as ferramentas, se houver).
    Devolve (conteudo do modelo, uso, finishReason). O conteudo e devolvido inteiro
    porque o historico precisa repetir as partes do modelo (functionCall e assinaturas)."""
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": contents,
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.2},
    }
    if tools:
        body["tools"] = tools
    data = _post(f"models/{GEN_MODEL}:generateContent", body, timeout=timeout)
    cand = (data.get("candidates") or [{}])[0]
    content = cand.get("content") or {}
    usage = data.get("usageMetadata", {})
    return (
        {"role": "model", "parts": content.get("parts") or []},
        {"tokens_in": usage.get("promptTokenCount", 0), "tokens_out": usage.get("candidatesTokenCount", 0)},
        cand.get("finishReason", ""),
    )


def generate(system, prompt, max_tokens):
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.2},
    }
    data = _post(f"models/{GEN_MODEL}:generateContent", body)
    cand = (data.get("candidates") or [{}])[0]
    parts = (cand.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts)
    usage = data.get("usageMetadata", {})
    return text, {
        "tokens_in": usage.get("promptTokenCount", 0),
        "tokens_out": usage.get("candidatesTokenCount", 0),
    }
