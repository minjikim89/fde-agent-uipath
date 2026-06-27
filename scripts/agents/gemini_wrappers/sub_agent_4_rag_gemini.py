"""
Sub-Agent 4 (Gemini brain) — RAG over AIID Chroma

입력: diagnosis dict (Sub-Agent 2 output)
출력: per-node retrieved AIID incidents (top-N)

Brain 사용:
  1. Query rewriting — node label + failure mode → 더 풍부한 retrieval query
  2. Result re-ranking — Chroma top-N에서 LLM이 relevance 재정렬 (optional)

[금지] GraphRAG hybrid 통합은 🅓이 별도 mode entry로 추가 — 본 wrapper는 vanilla Chroma만,
GraphRAG hook은 future_graphrag dict 자리에 placeholder.

Chroma 인프라 미설치 / 모델 로딩 실패 / Mock brain 시 graceful — empty list return.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from ._base import GeminiSubAgentBase


CHROMA_PATH = Path(__file__).parent.parent.parent / "data" / "chroma"


GEMINI_QUERY_REWRITE_TPL = """\
You are a retrieval query optimizer over the AIID (AI Incident Database) corpus.

Original node failure: "{failure_mode}" at node "{node_label}" (AI mode: {ai_mode}).

Rewrite this into a short retrieval query (max 25 words, English) that maximizes recall of
similar incidents. Focus on the failure mechanism, not the workflow context. Output ONLY the query.
"""


class SubAgent4RAGGemini(GeminiSubAgentBase):
    name = "sub_agent_4_rag_gemini"

    def __init__(self, brain=None, n_results: int = 5):
        super().__init__(brain)
        self.n_results = n_results
        self._chroma = None
        self._embed_model = None
        self._chroma_ready = False
        self._init_chroma()

    def _init_chroma(self):
        try:
            import chromadb
            from sentence_transformers import SentenceTransformer
            import torch
            device = "mps" if torch.backends.mps.is_available() else "cpu"
            self._embed_model = SentenceTransformer("BAAI/bge-m3", device=device)
            client = chromadb.PersistentClient(path=str(CHROMA_PATH))
            self._chroma = client.get_collection("aiid_incidents")
            self._chroma_ready = True
        except Exception:
            # graceful — chroma / sentence-transformers 미설치 시
            self._chroma_ready = False

    def _build_query(self, diagnosis: dict) -> str:
        node = diagnosis.get("node", {})
        cells_by = diagnosis.get("cells_by_axis", {})
        failure_mode = next(
            (c.get("primary_failure_mode") for cells in cells_by.values() for c in cells if c.get("primary_failure_mode")),
            "",
        )
        base = f"{node.get('function','')} {failure_mode} hallucination failure"
        if self.is_mock or not failure_mode:
            return base.strip()
        # LLM rewriting
        rewrite = self.llm(
            GEMINI_QUERY_REWRITE_TPL.format(
                failure_mode=failure_mode,
                node_label=node.get("function", "?"),
                ai_mode=node.get("ai_mode", "?"),
            ),
            fallback=base,
        )
        return rewrite or base

    def retrieve(self, diagnosis: dict) -> dict:
        query = self._build_query(diagnosis)
        if not self._chroma_ready:
            return {
                "node_id": diagnosis.get("node", {}).get("id"),
                "query": query,
                "incidents": [],
                "chroma_ready": False,
                "future_graphrag": None,  # 🅓 hook
            }
        emb = self._embed_model.encode([query], normalize_embeddings=True).tolist()[0]
        res = self._chroma.query(query_embeddings=[emb], n_results=self.n_results)
        incidents = []
        for i in range(len(res["ids"][0])):
            incidents.append({
                "id": res["ids"][0][i],
                "title": res["metadatas"][0][i].get("title", ""),
                "similarity": round(1 - res["distances"][0][i], 4),
                "date": res["metadatas"][0][i].get("date", ""),
            })
        return {
            "node_id": diagnosis.get("node", {}).get("id"),
            "query": query,
            "incidents": incidents,
            "chroma_ready": True,
            "future_graphrag": None,  # 🅓 mode entry — Sprint 🅓에서 graphrag retrieval로 보강 가능
        }

    def run(self, diagnoses: list[dict]) -> list[dict]:
        return [self.retrieve(d) for d in diagnoses]
