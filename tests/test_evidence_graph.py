"""EvidenceGraph 单元测试：关系检测、边持久化、NetworkX 查询（无需 API key）。

关键点：显式传入 embedding 单位向量控制 cosine 相似度；不同 claim 用不同
paper_id 绕开 EvidenceStore 的 put 去重。
"""

from __future__ import annotations

import math

from src.evidence.graph import EdgeRecord, EvidenceGraph, _canonical_pair
from src.evidence.schemas import Evidence
from src.evidence.store import EvidenceStore


class HashEmbedder:
    """占位 embedder（graph 需要 embedder.dim，测试中不实际编码）。"""

    dim = 8

    def encode(self, text: str) -> list[float]:
        return [0.0] * self.dim


def _vec(c: float) -> list[float]:
    """返回与 [1,0,0,0] 余弦为 c 的单位向量。"""
    s = math.sqrt(max(0.0, 1.0 - c * c))
    return [c, s, 0.0, 0.0]


class _Env:
    """共用一个 db/session 的 store + graph 环境。"""

    def __init__(self, tmp_path, session: str = "g1"):
        db = str(tmp_path / "graph.db")
        self.store = EvidenceStore(db_path=db, embedder=HashEmbedder(), session_id=session)
        self.graph = EvidenceGraph(db_path=db, embedder=HashEmbedder(), session_id=session, supports_threshold=0.75)

    def add(self, claim: str, paper: str, emb: list[float]) -> str:
        ev = Evidence(
            evidence_id="",
            query="q",
            paper_id=paper,
            paper_title=f"Paper {paper}",
            source_url=f"https://arxiv.org/pdf/{paper}",
            claim=claim,
            evidence_text="excerpt",
            confidence=0.8,
            topic="q",
            session_id=self.store.session_id,
            embedding=emb,
        )
        final_id = self.store.put(ev)
        self.graph.add_evidence(ev, evidence_id=final_id)
        return final_id


# ---------------------------------------------------------------------------
# 关系检测
# ---------------------------------------------------------------------------

def test_supports_edge(tmp_path):
    env = _Env(tmp_path)
    a = env.add("Method A is fast", "p1", _vec(1.0))
    b = env.add("Method B is also fast", "p2", _vec(0.8))
    rels = env.graph.get_relations(a)
    supports = [r for r in rels if r.relation == "SUPPORTS"]
    assert len(supports) == 1
    assert {supports[0].source_id, supports[0].target_id} == {a, b}
    assert supports[0].weight == 0.8


def test_contradicts_via_negation(tmp_path):
    env = _Env(tmp_path)
    a = env.add("Model A is fast", "p1", _vec(1.0))
    b = env.add("Model A is not fast", "p2", _vec(0.7))
    rels = env.graph.get_relations(a)
    contradicts = [r for r in rels if r.relation == "CONTRADICTS"]
    assert len(contradicts) == 1
    assert contradicts[0].weight == 0.7


def test_contradicts_via_antonym(tmp_path):
    env = _Env(tmp_path)
    a = env.add("Model size increases performance", "p1", _vec(1.0))
    b = env.add("Model size decreases performance", "p2", _vec(0.7))
    contradicts = env.graph.get_contradictions()
    assert len(contradicts) == 1


def test_extends_edge(tmp_path):
    env = _Env(tmp_path)
    a = env.add("Memory architecture aspect one", "p1", _vec(1.0))
    b = env.add("Memory architecture aspect two", "p2", _vec(0.7))
    rels = env.graph.get_relations(a)
    extends = [r for r in rels if r.relation == "EXTENDS"]
    assert len(extends) == 1
    assert extends[0].weight == 0.7


def _semantic(relations) -> list:
    return [r for r in relations if r.relation in ("SUPPORTS", "CONTRADICTS", "EXTENDS")]


def test_no_edge_below_threshold(tmp_path):
    env = _Env(tmp_path)
    a = env.add("Method A is fast", "p1", _vec(1.0))
    b = env.add("Completely unrelated topic", "p2", _vec(0.0))
    assert _semantic(env.graph.get_relations(a)) == []
    assert _semantic(env.graph.get_relations(b)) == []


def test_same_paper_self_pair_skipped(tmp_path):
    env = _Env(tmp_path)
    a = env.add("Claim from paper one", "p1", _vec(1.0))
    b = env.add("Similar claim same paper", "p1", _vec(0.8))
    assert _semantic(env.graph.get_relations(a)) == []  # 同论文配对被跳过


def test_canonical_pair():
    assert _canonical_pair("E-2", "E-1") == ("E-1", "E-2")
    assert _canonical_pair("E-1", "E-2") == ("E-1", "E-2")


def test_canonical_dedup_no_duplicate_rows(tmp_path):
    env = _Env(tmp_path)
    env.add("Claim one about topic", "p1", _vec(1.0))
    env.add("Claim two about topic", "p2", _vec(0.8))
    rows = env.graph._query_edges("g1", where="relation = 'SUPPORTS'")
    assert len(rows) == 1  # 只落库一行，无 (A,B)/(B,A) 重复


# ---------------------------------------------------------------------------
# 结构边与去重交互
# ---------------------------------------------------------------------------

def test_structural_edges(tmp_path):
    env = _Env(tmp_path)
    a = env.add("Claim about memory", "p1", _vec(1.0))
    rels = env.graph.get_relations(a)
    by_relation = {r.relation: r for r in rels}
    assert by_relation["SOURCED_FROM"].target_id == "p1"
    assert by_relation["SOURCED_FROM"].target_paper == "Paper p1"
    assert by_relation["ANSWERS"].target_id == "topic:q"


def test_dedup_hit_skips_scan(tmp_path):
    env = _Env(tmp_path)
    a = env.add("The exact same claim text", "p1", _vec(1.0))
    # 同 claim 再入库：put 去重命中，返回已有 id，graph 应跳过语义扫描
    b = env.add("The exact same claim text", "p1", _vec(1.0))
    assert b == a
    assert len(env.graph._query_edges("g1")) == 2  # 只有 SOURCED_FROM + ANSWERS，无重复


# ---------------------------------------------------------------------------
# 持久化 / NetworkX / 统计
# ---------------------------------------------------------------------------

def test_persistence_across_instances(tmp_path):
    db = str(tmp_path / "graph.db")
    s1 = EvidenceStore(db_path=db, embedder=HashEmbedder(), session_id="g1")
    g1 = EvidenceGraph(db_path=db, embedder=HashEmbedder(), session_id="g1")
    ev = Evidence(evidence_id="", query="q", paper_id="p1", paper_title="P", source_url="",
                  claim="Claim X", evidence_text="e", confidence=0.8, topic="q",
                  session_id="g1", embedding=_vec(1.0))
    final_id = s1.put(ev)
    g1.add_evidence(ev, evidence_id=final_id)

    g2 = EvidenceGraph(db_path=db, embedder=HashEmbedder(), session_id="g1")
    rels = g2.get_relations(final_id)
    assert {r.relation for r in rels} == {"SOURCED_FROM", "ANSWERS"}


def test_to_networkx(tmp_path):
    env = _Env(tmp_path)
    env.add("Claim A about x", "p1", _vec(1.0))
    env.add("Claim B about x", "p2", _vec(0.8))
    G = env.graph.to_networkx()
    # 节点：2 evidence + 2 paper + 1 topic
    assert G.number_of_nodes() == 5
    # 边：2×SOURCED_FROM + 2×ANSWERS + 1×SUPPORTS
    assert G.number_of_edges() == 5
    evidence_nodes = [n for n, d in G.nodes(data=True) if d.get("type") == "evidence"]
    assert len(evidence_nodes) == 2
    rels = {data["relation"] for _, _, data in G.edges(data=True)}
    assert {"SOURCED_FROM", "ANSWERS", "SUPPORTS"} <= rels


def test_graph_stats(tmp_path):
    env = _Env(tmp_path)
    env.add("Claim A about x", "p1", _vec(1.0))
    env.add("Claim B about x", "p2", _vec(0.8))
    stats = env.graph.graph_stats()
    assert stats["nodes"]["evidence"] == 2
    assert stats["nodes"]["paper"] == 2
    assert stats["nodes"]["topic"] == 1
    assert stats["edges"]["SUPPORTS"] == 1
    assert stats["edges"]["SOURCED_FROM"] == 2
    assert stats["edges"]["ANSWERS"] == 2


def test_export_json(tmp_path):
    env = _Env(tmp_path)
    env.add("Claim A about x", "p1", _vec(1.0))
    data = env.graph.export_json()
    assert isinstance(data["nodes"], list) and len(data["nodes"]) >= 3
    assert isinstance(data["edges"], list) and len(data["edges"]) >= 2
    types = {n["type"] for n in data["nodes"]}
    assert types == {"evidence", "paper", "topic"}


def test_get_relations_bidirectional(tmp_path):
    env = _Env(tmp_path)
    a = env.add("Claim A about x", "p1", _vec(1.0))
    b = env.add("Claim B about x", "p2", _vec(0.8))
    rels_of_b = env.graph.get_relations(b)
    supports = [r for r in rels_of_b if r.relation == "SUPPORTS"]
    assert len(supports) == 1  # B 侧也能查到与 A 的关系


def test_get_contradictions_and_supports_filters(tmp_path):
    env = _Env(tmp_path)
    env.add("Model A is fast", "p1", _vec(1.0))
    env.add("Model A is not fast", "p2", _vec(0.7))
    env.add("Another claim about topic", "p3", _vec(0.8))
    assert len(env.graph.get_contradictions()) == 1
    assert len(env.graph.get_supports()) == 1
    assert len(env.graph.get_extends()) == 0


def test_edge_record_to_dict():
    rec = EdgeRecord(
        session_id="s", source_id="E-1", target_id="E-2", relation="SUPPORTS", weight=0.8,
        source_claim="c1", target_claim="c2", source_paper="P1", target_paper="P2",
    )
    d = rec.to_dict()
    assert d["relation"] == "SUPPORTS"
    assert d["source_claim"] == "c1"
    assert d["target_paper"] == "P2"
