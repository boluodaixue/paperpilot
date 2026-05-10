"""
DAG (有向无环图) 数据结构与拓扑排序

规划器输出的子任务依赖关系用 DAG 表示，编排器按拓扑序调度执行。
使用 Kahn 算法进行拓扑排序，并支持按层分组以最大化并行度。
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Iterator


__all__ = ["DAG", "DAGCycleError"]


class DAGCycleError(Exception):
    """DAG 中存在环时抛出，提示规划器输出非法。"""
    pass


class DAG:
    """有向无环图：节点为 task_id，边表示依赖关系 (u -> v 表示 v 依赖 u)。

    设计说明:
      - 采用邻接表存储，兼顾内存效率和遍历速度
      - 拓扑排序使用 Kahn 算法，时间复杂度 O(V+E)
      - get_parallel_groups() 将节点按"执行层"分组，同层节点无依赖可并行
    """

    def __init__(self) -> None:
        self._nodes: set[str] = set()
        self._edges: dict[str, list[str]] = defaultdict(list)   # 邻接表: node -> successors
        self._in_degree: dict[str, int] = defaultdict(int)

    # ------------------------------------------------------------------
    # 增删查
    # ------------------------------------------------------------------

    def add_node(self, node_id: str) -> None:
        """添加节点；已存在则静默忽略。"""
        self._nodes.add(node_id)

    def add_edge(self, from_node: str, to_node: str) -> None:
        """添加有向边 from_node -> to_node (to_node 依赖 from_node)。

        自动添加缺失的节点，并更新入度。
        """
        if from_node == to_node:
            raise DAGCycleError(f"自环不允许: {from_node}")
        self._nodes.add(from_node)
        self._nodes.add(to_node)
        self._edges[from_node].append(to_node)
        self._in_degree[to_node] += 1
        # 确保 from_node 也在 _in_degree 中有条目（即使为 0）
        self._in_degree.setdefault(from_node, 0)

    def has_node(self, node_id: str) -> bool:
        return node_id in self._nodes

    def get_dependencies(self, node_id: str) -> list[str]:
        """返回直接依赖 node_id 的节点列表（即指向 node_id 的边）。"""
        deps: list[str] = []
        for src, dsts in self._edges.items():
            if node_id in dsts:
                deps.append(src)
        return deps

    def get_successors(self, node_id: str) -> list[str]:
        """返回 node_id 直接指向的后继节点。"""
        return list(self._edges.get(node_id, []))

    def __iter__(self) -> Iterator[str]:
        return iter(self._nodes)

    def __len__(self) -> int:
        return len(self._nodes)

    def __contains__(self, node_id: str) -> bool:
        return node_id in self._nodes

    # ------------------------------------------------------------------
    # 拓扑排序
    # ------------------------------------------------------------------

    def topological_sort(self) -> list[str]:
        """Kahn 算法拓扑排序，返回节点全序列表。

        Raises:
            DAGCycleError: 图中存在环时抛出。
        """
        in_deg = dict(self._in_degree)
        # 补充可能遗漏的节点（孤立节点入度为 0）
        for n in self._nodes:
            in_deg.setdefault(n, 0)

        queue: deque[str] = deque([n for n in self._nodes if in_deg.get(n, 0) == 0])
        result: list[str] = []

        while queue:
            node = queue.popleft()
            result.append(node)
            for succ in self._edges.get(node, []):
                in_deg[succ] -= 1
                if in_deg[succ] == 0:
                    queue.append(succ)

        if len(result) != len(self._nodes):
            # 找出环上的节点，便于调试
            remaining = self._nodes - set(result)
            raise DAGCycleError(
                f"DAG 中存在环，无法完成拓扑排序。剩余节点: {sorted(remaining)}"
            )
        return result

    def get_parallel_groups(self) -> list[list[str]]:
        """按"执行层"分组，返回可并行执行的节点组。

        每一层内的节点之间不存在依赖关系，可被并发调度。
        层的顺序即为拓扑序的批次。

        Returns:
            例如 [["A", "B"], ["C"], ["D"]] 表示 A/B 并行，然后 C，然后 D。
        """
        in_deg = dict(self._in_degree)
        for n in self._nodes:
            in_deg.setdefault(n, 0)

        groups: list[list[str]] = []
        current: list[str] = [n for n in self._nodes if in_deg.get(n, 0) == 0]
        # 按字典序稳定排序，保证确定性
        current.sort()
        visited: set[str] = set()

        while current:
            groups.append(current)
            visited.update(current)
            next_layer: list[str] = []
            for node in current:
                for succ in self._edges.get(node, []):
                    in_deg[succ] -= 1
                    if in_deg[succ] == 0 and succ not in visited:
                        next_layer.append(succ)
            next_layer.sort()
            current = next_layer

        # 安全检查
        if sum(len(g) for g in groups) != len(self._nodes):
            raise DAGCycleError("DAG 中存在环，无法计算并行分组")
        return groups

    def to_dict(self) -> dict:
        """序列化为字典，便于日志和持久化。"""
        return {
            "nodes": sorted(self._nodes),
            "edges": {k: sorted(v) for k, v in sorted(self._edges.items())},
        }

    def __repr__(self) -> str:
        return f"<DAG nodes={len(self._nodes)} edges={sum(len(v) for v in self._edges.values())}>"
