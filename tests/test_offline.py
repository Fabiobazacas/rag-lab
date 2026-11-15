"""Teste offline das funções Lambda: sem AWS, sem chave do Gemini, sem internet.

Substitui o boto3 por um S3/DynamoDB em memória e o Gemini por um servidor falso.
Uso (na raiz do repositório):  python tests/test_offline.py
"""
import hashlib
import io
import json
import math
import os
import sys
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lambda"))
sys.dont_write_bytecode = True  # não cria lambda/__pycache__ (entraria no zip)

os.environ.update(
    GEMINI_API_KEY="fake", GEMINI_MODEL="fake-gen", EMBED_MODEL="fake-embed",
    BUCKET="b", TOP_K="2", MAX_OUTPUT_TOKEN="64", LOG_TABLE="t",
)

# ---------- S3 e DynamoDB falsos ----------
STORE, LOG = {}, []


class NoSuchKey(Exception):
    pass


class FakeS3:
    class exceptions:
        NoSuchKey = NoSuchKey

    def get_paginator(self, _):
        return types.SimpleNamespace(paginate=lambda Bucket, Prefix: [
            {"Contents": [{"Key": k, "Size": len(STORE[k]), "ETag": '"' + hashlib.md5(STORE[k]).hexdigest() + '"'}
                           for k in sorted(STORE) if k.startswith(Prefix)]}])

    def get_object(self, Bucket, Key):
        if Key not in STORE:
            raise NoSuchKey(Key)
        return {"Body": io.BytesIO(STORE[Key])}

    def put_object(self, Bucket, Key, Body):
        STORE[Key] = Body


class FakeTable:
    def put_item(self, Item):
        LOG.append(Item)


fake_boto3 = types.ModuleType("boto3")
fake_boto3.client = lambda *_: FakeS3()
fake_boto3.resource = lambda *_: types.SimpleNamespace(Table=lambda _n: FakeTable())
sys.modules["boto3"] = fake_boto3

# ---------- Gemini falso ----------
MODE = {"fail": None, "finish": "STOP", "queries": []}  # fail: None | código HTTP; queries: buscas que o agente falso pede, uma rodada por item
PDF_CALLS = []  # uma entrada por PDF enviado ao Gemini
AGENT_REQS = []  # corpos das chamadas do agente (as que trazem 'tools' ou functionResponse)
PDF_TEXT = "Declaracao Universal dos Direitos Humanos. Artigo 1: todos os seres humanos nascem livres e iguais em dignidade e em direitos."


def _vec(text):
    """Vetor 'bag of words' determinístico: palavras iguais => vetores próximos."""
    v = [0.0] * 768
    for w in text.lower().split():
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 768] += 1
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def fake_urlopen(req, timeout=None):
    if MODE["fail"]:
        raise urllib.error.HTTPError(req.full_url, MODE["fail"], "x", {}, io.BytesIO(b'{"error":"fake"}'))
    body = json.loads(req.data)
    assert req.headers["X-goog-api-key"] == "fake"
    if "batchEmbedContents" in req.full_url:
        out = {"embeddings": [{"values": _vec(r["content"]["parts"][0]["text"])} for r in body["requests"]]}
    elif any("inline_data" in p for p in body["contents"][0]["parts"]):
        PDF_CALLS.append(body["generationConfig"]["maxOutputTokens"])
        out = {"candidates": [{"content": {"parts": [{"text": PDF_TEXT}]}, "finishReason": MODE["finish"]}],
               "usageMetadata": {"promptTokenCount": 2600, "candidatesTokenCount": 40}}
    else:
        rounds = sum(1 for c in body["contents"] if any("functionResponse" in p for p in c["parts"]))
        if "tools" in body or rounds:
            AGENT_REQS.append(body)
        if "tools" in body and rounds < len(MODE["queries"]):
            q = MODE["queries"][rounds]
            qs = q if isinstance(q, list) else [q]
            parts = [{"functionCall": {"name": "buscar_documentos", "args": {"consulta": x}}, "thoughtSignature": f"sig{rounds}"}
                     for x in qs]
            out = {"candidates": [{"content": {"role": "model", "parts": parts}}],
                   "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 7}}
        else:
            out = {"candidates": [{"content": {"parts": [{"text": "Resposta [docs/politica-exemplo.txt#1]"}]}}],
                   "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}}
    return io.BytesIO(json.dumps(out).encode())


patcher = mock.patch("urllib.request.urlopen", fake_urlopen)
patcher.start()

import agent  # noqa: E402
import ask  # noqa: E402
import ingest  # noqa: E402


def post(question):
    r = ask.handler({"body": json.dumps({"question": question})}, None)
    return r["statusCode"], json.loads(r["body"])


def post_agent(question):
    r = agent.handler({"body": json.dumps({"question": question})}, None)
    return r["statusCode"], json.loads(r["body"])


class Offline(unittest.TestCase):
    def setUp(self):
        STORE.clear()
        LOG.clear()
        PDF_CALLS.clear()
        AGENT_REQS.clear()
        MODE["fail"] = None
        MODE["finish"] = "STOP"
        MODE["queries"] = []
        ask._index = None
        agent._index = None
        agent.MAX_STEPS = 4
        agent.DEADLINE_S = 22

    def load_docs(self):
        for p in (ROOT / "docs-exemplo").iterdir():
            STORE[f"docs/{p.name}"] = p.read_bytes()

    def test_1_sem_indice_devolve_409(self):
        self.assertEqual(post("oi")[0], 409)
        self.assertEqual(LOG[0]["status"], "ok")  # registrado mesmo assim

    def test_2_ingestao_sem_documentos(self):
        self.assertFalse(ingest.handler({}, None)["ok"])

    def test_3_ingestao_e_consulta(self):
        self.load_docs()
        res = ingest.handler({}, None)
        self.assertTrue(res["ok"] and res["chunks"] >= 1)
        status, body = post("Qual o prazo de resposta de uma reclamação formal?")
        self.assertEqual(status, 200)
        self.assertTrue(body["sources"][0].startswith("docs/politica-exemplo.txt#"))
        self.assertEqual((body["tokens_in"], body["tokens_out"]), (10, 5))
        self.assertEqual(LOG[-1]["status"], "ok")
        self.assertIn("latency_ms", LOG[-1])

    def test_4_pergunta_vazia_devolve_400(self):
        self.assertEqual(post("  ")[0], 400)

    def test_5_chave_invalida_devolve_502_e_registra(self):
        self.load_docs()
        ingest.handler({}, None)
        MODE["fail"] = 400
        self.assertEqual(post("prazo")[0], 502)
        self.assertEqual(LOG[-1]["status"], "gemini_400")

    def test_6_cota_esgotada_devolve_503(self):
        self.load_docs()
        ingest.handler({}, None)
        MODE["fail"] = 429
        self.assertEqual(post("prazo")[0], 503)

    def test_7_chunks_grandes_sao_divididos(self):
        pieces = ingest.split("palavra " * 1000)
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(len(p) <= ingest.CHUNK for p in pieces))

    # ---------- PDF ----------
    def test_8_pdf_e_transcrito_uma_vez_e_fica_em_cache(self):
        STORE["docs/dudh.pdf"] = b"%PDF-1.7 fake"
        res = ingest.handler({}, None)
        self.assertTrue(res["ok"])
        self.assertEqual(res["transcritos"], ["docs/dudh.pdf"])
        self.assertEqual((res["tokens_in"], res["tokens_out"]), (2600, 40))
        self.assertEqual(len(PDF_CALLS), 1)
        self.assertEqual(PDF_CALLS[0], ingest.MAX_PDF_TOKENS)
        self.assertEqual(sum(k.startswith("cache/docs/dudh.pdf.") for k in STORE), 1)

        res2 = ingest.handler({}, None)  # segunda execução: usa o cache
        self.assertTrue(res2["ok"])
        self.assertEqual(len(PDF_CALLS), 1)
        self.assertNotIn("tokens_in", res2)

        status, body = post("Todos os seres humanos nascem livres e iguais?")
        self.assertEqual(status, 200)
        self.assertTrue(body["sources"][0].startswith("docs/dudh.pdf#"))

    def test_9_pdf_alterado_e_transcrito_de_novo(self):
        STORE["docs/dudh.pdf"] = b"%PDF-1.7 versao 1"
        ingest.handler({}, None)
        STORE["docs/dudh.pdf"] = b"%PDF-1.7 versao 2"
        ingest.handler({}, None)
        self.assertEqual(len(PDF_CALLS), 2)

    def test_10_pdf_grande_demais_e_ignorado(self):
        STORE["docs/enorme.pdf"] = b"0" * (ingest.MAX_PDF_BYTES + 1)
        STORE["docs/ok.txt"] = b"texto simples"
        res = ingest.handler({}, None)
        self.assertTrue(res["ok"])
        self.assertEqual(res["ignorados"][0]["arquivo"], "docs/enorme.pdf")
        self.assertEqual(PDF_CALLS, [])

    def test_11_cota_esgotada_no_pdf_nao_derruba_a_ingestao(self):
        STORE["docs/dudh.pdf"] = b"%PDF-1.7 fake"
        MODE["fail"] = 429
        res = ingest.handler({}, None)  # só há o PDF: sem trechos
        self.assertFalse(res["ok"])
        self.assertEqual(res["ignorados"][0]["motivo"], "cota do Gemini esgotada")
        self.assertFalse(any(k.startswith("cache/") for k in STORE))

    def test_12_transcricao_cortada_avisa_e_nao_vai_para_o_cache(self):
        STORE["docs/dudh.pdf"] = b"%PDF-1.7 fake"
        MODE["finish"] = "MAX_TOKENS"
        res = ingest.handler({}, None)
        self.assertTrue(res["ok"])
        self.assertEqual(res["truncados"], ["docs/dudh.pdf"])
        self.assertFalse(any(k.startswith("cache/") for k in STORE))

    def test_13_sem_tempo_o_pdf_fica_pendente(self):
        STORE["docs/dudh.pdf"] = b"%PDF-1.7 fake"
        STORE["docs/ok.txt"] = b"texto simples"
        pouco_tempo = types.SimpleNamespace(get_remaining_time_in_millis=lambda: 20_000)
        res = ingest.handler({}, pouco_tempo)
        self.assertTrue(res["ok"])
        self.assertEqual(res["pendentes"], ["docs/dudh.pdf"])
        self.assertEqual(PDF_CALLS, [])

    # ---------- Agente ----------
    def load_all(self):
        self.load_docs()
        STORE["docs/dudh.pdf"] = b"%PDF-1.7 fake"
        ingest.handler({}, None)

    def test_14_agente_faz_duas_buscas_e_junta_as_fontes(self):
        self.load_all()
        MODE["queries"] = ["prazo de resposta de uma reclamação formal", "todos os seres humanos nascem livres e iguais"]
        status, body = post_agent("Compare a política de reclamação com o artigo 1 da DUDH")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["steps"]), 2)
        self.assertTrue(body["steps"][0]["sources"][0].startswith("docs/politica-exemplo.txt#"))
        self.assertTrue(body["steps"][1]["sources"][0].startswith("docs/dudh.pdf#"))
        self.assertEqual(len(body["sources"]), len(set(body["sources"])))  # sem repetição
        self.assertTrue(any(s.startswith("docs/dudh.pdf#") for s in body["sources"]))
        self.assertEqual((body["tokens_in"], body["tokens_out"]), (100 + 100 + 10, 7 + 7 + 5))
        self.assertEqual(LOG[-1]["mode"], "agent")
        self.assertEqual(LOG[-1]["searches"], 2)
        # o histórico devolve a chamada do modelo (com a assinatura) seguida do resultado
        ultima = AGENT_REQS[-1]["contents"]
        self.assertEqual(ultima[1]["parts"][0]["thoughtSignature"], "sig0")
        self.assertIn("functionResponse", ultima[2]["parts"][0])

    def test_15_agente_pode_responder_sem_buscar(self):
        self.load_all()
        status, body = post_agent("oi")
        self.assertEqual(status, 200)
        self.assertEqual(body["steps"], [])
        self.assertEqual(body["sources"], [])

    def test_16_limite_de_passos_forca_a_resposta_final_sem_ferramenta(self):
        self.load_all()
        agent.MAX_STEPS = 2
        MODE["queries"] = ["prazo"] * 10  # o modelo nunca para sozinho
        status, body = post_agent("prazo")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["steps"]), 2)
        self.assertNotIn("tools", AGENT_REQS[-1])
        self.assertTrue(body["answer"])

    def test_17_prazo_estourado_pula_a_ferramenta(self):
        self.load_all()
        agent.DEADLINE_S = -1
        MODE["queries"] = ["prazo"]
        status, body = post_agent("prazo")
        self.assertEqual(status, 200)
        self.assertEqual(body["steps"], [])

    def test_18_agente_limita_buscas_por_rodada(self):
        self.load_all()
        MODE["queries"] = [["a", "b", "c", "d", "e"]]
        _, body = post_agent("muitas buscas")
        self.assertEqual(len(body["steps"]), agent.MAX_CALLS_PER_STEP)

    def test_19_agente_erros_http(self):
        self.assertEqual(post_agent("oi")[0], 409)  # sem índice
        self.assertEqual(post_agent("  ")[0], 400)
        self.load_all()
        MODE["fail"] = 429
        self.assertEqual(post_agent("prazo")[0], 503)
        MODE["fail"] = 400
        self.assertEqual(post_agent("prazo")[0], 502)
        self.assertEqual(LOG[-1]["status"], "gemini_400")


if __name__ == "__main__":
    unittest.main(verbosity=2)
