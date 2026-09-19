"""最小冒烟测试：验证求解器核心约束与前端服务可用性。

运行：python -m tests.test_smoke
（仅使用标准库，不依赖 pytest）
"""
from __future__ import annotations

import os
import tempfile

# 测试前指定独立数据库
_tmp = tempfile.mkdtemp(prefix="review-test-")
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")

from app.db import _connect, init_db  # noqa: E402
from app.mcmf import MinCostMaxFlow  # noqa: E402
from app.solver import solve  # noqa: E402


def test_mcmf_known_case() -> None:
    net = MinCostMaxFlow(6)
    net.add_edge(0, 1, 2, 0)
    net.add_edge(0, 2, 2, 0)
    net.add_edge(1, 3, 1, 1)
    net.add_edge(1, 4, 2, 3)
    net.add_edge(2, 3, 2, 1)
    net.add_edge(3, 5, 2, 2)
    net.add_edge(4, 5, 2, 1)
    flow, cost = net.solve(0, 5)
    assert (flow, cost) == (4, 14)


def test_demo_solve_constraints() -> None:
    init_db(seed_demo=True)
    conn = _connect()
    try:
        sol = solve(conn)
        # 演示数据刻意制造 1 篇未满足
        assert not sol["fully_satisfied"]
        assert len(sol["unmet"]) == 1

        # 冲突边绝不出现在分配中
        conflicts = {
            (r["m_id"], r["r_id"])
            for r in conn.execute("SELECT m_id, r_id FROM conflicts")
        }
        used = {
            (r["m_id"], r["r_id"])
            for r in conn.execute("SELECT m_id, r_id FROM assignments")
        }
        assert not (conflicts & used)

        # 每稿不超需求、每人不超容量（无锁定时）
        req = {r["id"]: r["required_reviews"]
               for r in conn.execute("SELECT id, required_reviews FROM manuscripts")}
        cap = {r["id"]: r["capacity"]
               for r in conn.execute("SELECT id, capacity FROM reviewers")}
        per_m: dict[int, int] = {}
        per_r: dict[int, int] = {}
        for m_id, r_id in used:
            per_m[m_id] = per_m.get(m_id, 0) + 1
            per_r[r_id] = per_r.get(r_id, 0) + 1
        assert all(per_m.get(m, 0) <= n for m, n in req.items())
        assert all(per_r.get(r, 0) <= n for r, n in cap.items())
    finally:
        conn.close()


def test_locks_occupy_capacity() -> None:
    conn = _connect()
    try:
        conn.execute("UPDATE assignments SET locked=1 WHERE m_id=1 AND r_id=2")
        conn.commit()
        sol = solve(conn)
        assert sol["locked_count"] == 1
        # 被锁评审剩余容量被锁定占用（自由分配数减少 1）
        free_for_r2 = conn.execute(
            "SELECT COUNT(*) AS c FROM assignments WHERE r_id=2 AND locked=0"
        ).fetchone()["c"]
        cap_r2 = conn.execute(
            "SELECT capacity FROM reviewers WHERE id=2").fetchone()["capacity"]
        assert free_for_r2 == cap_r2 - 1
        conn.execute("DELETE FROM assignments WHERE locked=1")
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    test_mcmf_known_case()
    test_demo_solve_constraints()
    test_locks_occupy_capacity()
    print("所有冒烟测试通过 ✅")
