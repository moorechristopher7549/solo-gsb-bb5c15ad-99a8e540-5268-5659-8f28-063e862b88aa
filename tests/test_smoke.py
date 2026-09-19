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

from app.db import _connect, init_db, set_setting  # noqa: E402
from app.mcmf import MinCostMaxFlow  # noqa: E402
from app.solver import precheck_new_lock, solve  # noqa: E402


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


def _reset_demo(conn) -> None:
    conn.execute("DELETE FROM assignments")
    conn.execute("DELETE FROM conflicts")
    conn.execute("DELETE FROM manuscripts")
    conn.execute("DELETE FROM reviewers")
    conn.commit()
    from app.db import seed_demo_data
    seed_demo_data(conn)
    conn.commit()


def test_institution_cap_enforced() -> None:
    """开启每稿同机构上限后，任一稿件的同机构人数不得超过上限；无机构者不受限。"""
    conn = _connect()
    try:
        _reset_demo(conn)
        set_setting(conn, "max_same_institution", 2)
        conn.commit()
        sol = solve(conn)

        inst_by_m: dict[int, dict[str, int]] = {}
        for row in conn.execute(
            "SELECT a.m_id AS m_id, rv.institution AS institution "
            "FROM assignments a JOIN reviewers rv ON a.r_id = rv.id"
        ):
            if row["institution"]:
                inst_by_m.setdefault(row["m_id"], {})
                inst_by_m[row["m_id"]][row["institution"]] = \
                    inst_by_m[row["m_id"]].get(row["institution"], 0) + 1
        for groups in inst_by_m.values():
            assert all(cnt <= 2 for cnt in groups.values())

        # 机构构成按稿给出，且字段齐全
        comp = sol["institution_composition"]["1"]
        a_group = next(g for g in comp if g["institution"] == "A大学")
        assert a_group["count"] <= 2 and a_group["locked_count"] == 0
        assert sol["max_same_institution"] == 2
    finally:
        conn.close()


def test_institution_limit_zero_disabled() -> None:
    """上限 0 = 关闭：求解结果与既有行为一致，且不出现机构超限警告。"""
    conn = _connect()
    try:
        _reset_demo(conn)
        set_setting(conn, "max_same_institution", 0)
        conn.commit()
        sol = solve(conn)
        assert not any(w["code"] == "institution_lock_exceeds_limit"
                       for w in sol["warnings"])
        # 关闭时同机构人数不受限（M1 可同时容纳 3 名 A 大学评审）
        comp = {g["institution"]: g["count"]
                for g in sol["institution_composition"].get("1", [])}
        assert comp.get("A大学", 0) <= 3
    finally:
        conn.close()


def test_lock_precheck_institution_409() -> None:
    """新增锁定会超过机构上限时，precheck 必须拒绝（路由层映射为 409）。"""
    conn = _connect()
    try:
        _reset_demo(conn)
        set_setting(conn, "max_same_institution", 2)
        # M1 已锁 r1、r2（均为 A大学）
        conn.execute(
            "INSERT INTO assignments(m_id, r_id, locked) VALUES (1, 1, 1)")
        conn.execute(
            "INSERT INTO assignments(m_id, r_id, locked) VALUES (1, 2, 1)")
        conn.commit()
        # 再锁 r4（赵博士，A大学）-> 拒绝
        try:
            precheck_new_lock(conn, 1, 4)
            raise AssertionError("应抛出机构超限异常")
        except ValueError:
            pass
        # 锁无机构评审 r6（孙老师）不受机构上限约束（仅受需求约束）
        precheck_new_lock(conn, 1, 6)
        # 重复提交已锁定的行幂等通过
        precheck_new_lock(conn, 1, 1)
    finally:
        conn.close()


def test_over_limit_historical_lock_kept_and_blocks_new() -> None:
    """历史锁定已超机构上限：保留锁定、警告，且求解不再向该机构分配。"""
    conn = _connect()
    try:
        _reset_demo(conn)
        set_setting(conn, "max_same_institution", 2)
        conn.execute("UPDATE manuscripts SET required_reviews=4 WHERE id=1")
        for rid in (1, 2, 4):  # 3 名 A 大学评审全部锁定 -> 超限
            conn.execute(
                "INSERT INTO assignments(m_id, r_id, locked) VALUES (1, ?, 1) "
                "ON CONFLICT(m_id, r_id) DO UPDATE SET locked=1", (rid,))
        conn.commit()
        sol = solve(conn)

        assert any(w["code"] == "institution_lock_exceeds_limit"
                   and w["manuscript_id"] == 1 for w in sol["warnings"])
        # 三把锁全部保留
        locked = conn.execute(
            "SELECT COUNT(*) AS c FROM assignments WHERE m_id=1 AND locked=1"
        ).fetchone()["c"]
        assert locked == 3
        # 新增的自由分配不允许再来自 A 大学
        free_rows = conn.execute(
            "SELECT rv.institution AS institution FROM assignments a "
            "JOIN reviewers rv ON a.r_id = rv.id "
            "WHERE a.m_id=1 AND a.locked=0").fetchall()
        assert all(r["institution"] != "A大学" for r in free_rows)
        g = next(x for x in sol["institution_composition"]["1"]
                 if x["institution"] == "A大学")
        assert g["over_limit"] and g["count"] == 3
    finally:
        conn.close()


def test_old_db_migration_adds_institution() -> None:
    """旧版数据库（reviewers 无 institution 列）迁移后保留数据且可正常求解。"""
    import os
    import sqlite3
    import tempfile

    old_db = os.path.join(tempfile.mkdtemp(prefix="review-mig-"), "old.db")
    raw = sqlite3.connect(old_db)
    raw.executescript(
        "CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);"
        "CREATE TABLE manuscripts(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, "
        "topics TEXT DEFAULT '[]', required_reviews INTEGER DEFAULT 3);"
        "CREATE TABLE reviewers(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, "
        "topics TEXT DEFAULT '[]', capacity INTEGER DEFAULT 3);"
        "CREATE TABLE conflicts(m_id INTEGER, r_id INTEGER, PRIMARY KEY(m_id, r_id));"
        "CREATE TABLE assignments(id INTEGER PRIMARY KEY AUTOINCREMENT, m_id INTEGER, "
        "r_id INTEGER, locked INTEGER DEFAULT 0, UNIQUE(m_id, r_id));"
    )
    raw.execute("INSERT INTO reviewers(name, capacity) VALUES('老评审', 9)")
    raw.commit()
    raw.close()

    import app.db as dbmod
    old_path = dbmod.DB_PATH
    dbmod.DB_PATH = old_db
    try:
        dbmod.init_db(seed_demo=False)
        conn = dbmod._connect()
        try:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(reviewers)")}
            assert "institution" in cols
            row = conn.execute(
                "SELECT name, capacity, institution FROM reviewers").fetchone()
            assert (row["name"], row["capacity"], row["institution"]) == \
                ("老评审", 9, None)
            solve(conn)  # 迁移后的库可正常求解
        finally:
            conn.close()
    finally:
        dbmod.DB_PATH = old_path


if __name__ == "__main__":
    test_mcmf_known_case()
    test_demo_solve_constraints()
    test_locks_occupy_capacity()
    test_institution_cap_enforced()
    test_institution_limit_zero_disabled()
    test_lock_precheck_institution_409()
    test_over_limit_historical_lock_kept_and_blocks_new()
    test_old_db_migration_adds_institution()
    print("所有冒烟测试通过 ✅")
