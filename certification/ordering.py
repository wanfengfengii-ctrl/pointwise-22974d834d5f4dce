"""纯核证算法：在证据允许的全部完整原行顺序上求目标工段最长连续时长。

证据模型
--------
* 每页原行按页内原序串成页链（页内原序可靠）。
* 现场登记的跨页关系是页链之间的额外有向边（跨页只有部分先后）。
* 录入顺序、页号、发页序（旧账页序）都不参与定序。

对满足页链与跨页边的每个完整原行顺序（即该 DAG 的线性扩展），按原码表已
译出的工段计算目标工段的最长连续时长：相邻同工段时长相加，其他工段的原行
即使毫秒为零也会断开。返回所有顺序所得最长连续时长的最小值与最大值，各附
一份可复算的完整原行顺序。

求解
----
前沿状态为各页页首位置（页内前驱由前沿天然保证，跨页前驱另行校验），前沿
按已用原行数 BFS，天然是拓扑序。每个前沿保留一个关于 (best, current) 的
帕累托前沿：

* best = 该前缀已完成片段中的最长目标连续时长；
* current = 前缀末尾正在延续的目标连续时长。

求最大保留不被“两项都不小于”支配的对，求最小保留不被“两项都不大于”支配
的对。每个条目带 parent 指向上一层条目与本次原行，末层条目重建即完整顺序。
规模上限四页、每页十二条（共 48 行）。
"""
from collections import deque
from dataclasses import dataclass

from certification.errors import (
    DanglingRelationError,
    RelationConflictError,
)


@dataclass(frozen=True, slots=True)
class OrderRow:
    """核证所用原行：出处（页、行位）与按原码表译出的工段、毫秒。"""
    page_id: str
    line_no: int
    segment: str
    milliseconds: int


@dataclass(frozen=True, slots=True)
class OrderEdge:
    before_page: str
    before_line: int
    after_page: str
    after_line: int


@dataclass(frozen=True, slots=True)
class ContinuityResult:
    min_duration: int
    max_duration: int
    min_order: tuple[OrderRow, ...]
    max_order: tuple[OrderRow, ...]
    edges: tuple[OrderEdge, ...]


def _index_rows(rows: tuple[OrderRow, ...]) -> dict[tuple[str, int], int]:
    index: dict[tuple[str, int], int] = {}
    for position, row in enumerate(rows):
        key = (row.page_id, row.line_no)
        if key in index:
            raise DanglingRelationError('原行出处重复')
        index[key] = position
    return index


def _build_pages(rows: tuple[OrderRow, ...]) -> list[list[int]]:
    by_page: dict[str, list[int]] = {}
    for position, row in enumerate(rows):
        by_page.setdefault(row.page_id, []).append(position)
    for page_id, positions in by_page.items():
        line_numbers = [rows[position].line_no for position in positions]
        if line_numbers != sorted(line_numbers) or len(set(line_numbers)) != len(line_numbers):
            raise RelationConflictError('页内原行位不可靠')
    return [by_page[page_id] for page_id in sorted(by_page)]


def evaluate_continuity(
    rows: tuple[OrderRow, ...],
    relations: tuple[OrderEdge, ...],
    target_segment: str,
) -> ContinuityResult:
    row_index = _index_rows(rows)

    edges: list[OrderEdge] = []
    seen_edges: set[tuple[str, int, str, int]] = set()
    for relation in relations:
        key = (relation.before_page, relation.before_line,
               relation.after_page, relation.after_line)
        if key in seen_edges:
            continue
        seen_edges.add(key)
        if (relation.before_page, relation.before_line) not in row_index:
            raise DanglingRelationError(
                f'关系前序原行缺失：{relation.before_page} 第 {relation.before_line} 行')
        if (relation.after_page, relation.after_line) not in row_index:
            raise DanglingRelationError(
                f'关系后序原行缺失：{relation.after_page} 第 {relation.after_line} 行')
        if relation.before_page == relation.after_page:
            raise RelationConflictError('页内先后不得登记为跨页关系')
        edges.append(relation)

    pages = _build_pages(rows)
    page_count = len(pages)
    page_of = [0] * len(rows)
    rank_of = [0] * len(rows)
    for page_no, positions in enumerate(pages):
        for rank, position in enumerate(positions):
            page_of[position] = page_no
            rank_of[position] = rank

    cross_predecessors: list[set[int]] = [set() for _ in rows]
    for edge in edges:
        before = row_index[(edge.before_page, edge.before_line)]
        after = row_index[(edge.after_page, edge.after_line)]
        cross_predecessors[after].add(before)

    _reject_if_cyclic(rows, pages, edges, row_index)

    if not rows:
        empty = ()
        return ContinuityResult(0, 0, empty, empty, tuple(edges))

    target_flags = [row.segment == target_segment for row in rows]
    min_value, min_positions = _solve(
        pages, page_of, rank_of, cross_predecessors, rows, target_flags, maximize=False)
    max_value, max_positions = _solve(
        pages, page_of, rank_of, cross_predecessors, rows, target_flags, maximize=True)
    return ContinuityResult(
        min_value, max_value,
        tuple(rows[position] for position in min_positions),
        tuple(rows[position] for position in max_positions),
        tuple(edges),
    )


def _reject_if_cyclic(rows, pages, edges, row_index) -> None:
    indegree = [0] * len(rows)
    adjacency: list[list[int]] = [[] for _ in rows]
    for positions in pages:
        for earlier, later in zip(positions, positions[1:]):
            adjacency[earlier].append(later)
            indegree[later] += 1
    for edge in edges:
        before = row_index[(edge.before_page, edge.before_line)]
        after = row_index[(edge.after_page, edge.after_line)]
        adjacency[before].append(after)
        indegree[after] += 1
    queue = deque(position for position, degree in enumerate(indegree) if degree == 0)
    visited = 0
    while queue:
        position = queue.popleft()
        visited += 1
        for later in adjacency[position]:
            indegree[later] -= 1
            if indegree[later] == 0:
                queue.append(later)
    if visited != len(rows):
        raise RelationConflictError('先后关系存在矛盾（有环），不能确定跨页先后')


def _solve(pages, page_of, rank_of, cross_predecessors, rows, target_flags, maximize):
    """帕累托前沿 DP。

    前沿 -> 条目列表，每条目为 (best, current, parent_entry, chosen_position)；
    首层条目 parent_entry 为 None。求最大保留 (best,current) 不被支配的对，
    求最小对称处理。
    """
    page_count = len(pages)
    page_sizes = tuple(len(page) for page in pages)
    zero = tuple(0 for _ in range(page_count))
    # 根条目：(best, current, parent, position)
    fronts: dict[tuple[int, ...], list[tuple]] = {zero: [(0, 0, None, -1)]}
    queue = deque([zero])

    while queue:
        front = queue.popleft()
        entries = fronts[front]
        for page_no in range(page_count):
            head = front[page_no]
            if head >= page_sizes[page_no]:
                continue
            candidate = pages[page_no][head]
            if any(front[page_of[predecessor]] <= rank_of[predecessor]
                   for predecessor in cross_predecessors[candidate]):
                continue
            next_front = tuple(
                head + 1 if index == page_no else value for index, value in enumerate(front))
            generated = []
            is_target = target_flags[candidate]
            weight = rows[candidate].milliseconds
            for best, current, parent, _ in entries:
                if is_target:
                    new_current = current + weight
                    new_best = best if best >= new_current else new_current
                else:
                    new_current = 0
                    new_best = best
                generated.append((new_best, new_current, (best, current, parent, _), candidate))
            existing = fronts.get(next_front)
            if existing is None:
                fronts[next_front] = generated
                queue.append(next_front)
            else:
                fronts[next_front] = _prune(existing, generated, maximize)

    final_entries = fronts[page_sizes]
    if maximize:
        chosen = max(final_entries, key=lambda entry: (entry[0], entry[1]))
    else:
        chosen = min(final_entries, key=lambda entry: (entry[0], entry[1]))
    return chosen[0], _reconstruct(chosen)


def _prune(existing, generated, maximize):
    """合并两组条目并按支配关系裁剪，保留帕累托前沿。"""
    if maximize:
        def dominated(a, b):
            # a 被 b 支配：b 两项都不小于 a，且至少一项更大
            return b[0] >= a[0] and b[1] >= a[1] and (b[0] > a[0] or b[1] > a[1])
    else:
        def dominated(a, b):
            # a 被 b 支配：b 两项都不大于 a，且至少一项更小
            return b[0] <= a[0] and b[1] <= a[1] and (b[0] < a[0] or b[1] < a[1])

    keep: list[tuple] = []
    for candidate in existing + generated:
        if any(dominated(candidate, keeper) for keeper in keep):
            continue
        keep = [keeper for keeper in keep if not dominated(keeper, candidate)]
        # 完全相同的 (best,current) 保留先到者，避免重复膨胀。
        if not any(keeper[0] == candidate[0] and keeper[1] == candidate[1] for keeper in keep):
            keep.append(candidate)
    return keep


def _reconstruct(entry) -> tuple[int, ...]:
    positions: list[int] = []
    best, current, parent, position = entry
    while parent is not None:
        positions.append(position)
        best, current, parent, position = parent
    positions.reverse()
    return tuple(positions)
