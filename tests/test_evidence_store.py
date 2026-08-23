"""EvidenceStore 单元测试：持久化、去重、session 隔离、ID 序列（无需 API key）。"""

from __future__ import annotations

import hashlib
import random

from src.evidence.schemas import Evidence
from src.evidence.store import EvidenceStore


class HashEmbedder:
    """确定性 hash 向量器（与真实 Embedder fallback 一致），避免加载模型。"""

    dim = 8

    def encode(self, text: str) -> list[float]:
        seed = int(hashlib.md5(text.encode("utf-8")).hexdigest(), 16) % (2**31)
        rng = random.Random(seed)
        vec = [rng.gauss(0.0, 1.0) for _ in range(self.dim)]
        norm = sum(v * v for v in vec) ** 0.5
        return [v / norm for v in vec]


def _make_evidence(session: str = "s1", paper: str = "p1", claim: str = "Claim X") -> Evidence:
    return Evidence(
        evidence_id="",
        query="query",
        paper_id=paper,
        paper_title="Paper X",
        source_url="https://arxiv.org/pdf/p1",
        claim=claim,
        evidence_text="verbatim excerpt",
        confidence=0.8,
        topic="query",
        session_id=session,
    )


def _store(tmp_path, session: str = "s1") -> EvidenceStore:
    return EvidenceStore(
        db_path=str(tmp_path / "test.db"),
        embedder=HashEmbedder(),
        session_id=session,
    )


def test_put_get_roundtrip(tmp_path):
    store = _store(tmp_path)
    ev = _make_evidence()
    eid = store.put(ev)
    assert eid == "E-1"

    loaded = store.get_all()
    assert len(loaded) == 1
    assert loaded[0].evidence_id == "E-1"
    assert loaded[0].claim == "Claim X"
    assert loaded[0].paper_id == "p1"
    assert loaded[0].source_url == "https://arxiv.org/pdf/p1"
    assert len(loaded[0].embedding) == 8


def test_evidence_id_sequence(tmp_path):
    store = _store(tmp_path)
    e1 = store.put(_make_evidence(claim="Claim A"))
    e2 = store.put(_make_evidence(claim="Claim B"))
    e3 = store.put(_make_evidence(claim="Claim C"))
    assert [e1, e2, e3] == ["E-1", "E-2", "E-3"]


def test_dedup_same_claim_same_paper(tmp_path):
    store = _store(tmp_path)
    store.put(_make_evidence(claim="The exact same claim text"))
    eid2 = store.put(_make_evidence(claim="The exact same claim text"))
    assert eid2 == "E-1"  # 重复，保留原 ID
    assert store.count() == 1


def test_distinct_claims_both_stored(tmp_path):
    store = _store(tmp_path)
    store.put(_make_evidence(claim="Completely different claim one"))
    store.put(_make_evidence(claim="Completely different claim two"))
    assert store.count() == 2


def test_session_isolation(tmp_path):
    s1 = _store(tmp_path, session="s1")
    s2 = _store(tmp_path, session="s2")
    s1.put(_make_evidence(claim="Session one claim"))
    s2.put(_make_evidence(claim="Session two claim"))
    assert s1.count() == 1
    assert s2.count() == 1
    assert s1.get_all()[0].session_id == "s1"
    assert s2.get_all()[0].session_id == "s2"


def test_empty_session_auto_generates_id(tmp_path):
    store = _store(tmp_path, session="")
    assert store.session_id.startswith("run-")


def test_get_by_paper(tmp_path):
    store = _store(tmp_path)
    store.put(_make_evidence(paper="p1", claim="Claim on paper one"))
    store.put(_make_evidence(paper="p2", claim="Claim on paper two"))
    papers = store.get_by_paper("p1")
    assert len(papers) == 1
    assert papers[0].paper_id == "p1"


def test_persistence_across_instances(tmp_path):
    db = str(tmp_path / "test.db")
    s1 = EvidenceStore(db_path=db, embedder=HashEmbedder(), session_id="s1")
    s1.put(_make_evidence(claim="Persistent claim"))
    s2 = EvidenceStore(db_path=db, embedder=HashEmbedder(), session_id="s1")
    assert s2.count() == 1
    assert s2.get_all()[0].claim == "Persistent claim"
    # ID 序列继续递增
    assert s2.put(_make_evidence(claim="Another persistent claim")) == "E-2"
