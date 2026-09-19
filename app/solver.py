"""评审分配求解器：最小费用最大流。

网络模型
========
节点：源点 S -> 稿件 M_i -> 评审人 R_j -> 汇点 T

* S -> M_i：容量 = 该稿尚缺的评审数（需求扣除已锁定分配）
* M_i -> R_j：容量 1；冲突边、与锁定分配重复的边一律不建
* R_j -> T：把容量拆成若干条单位“负载槽位”并行边，
  第 k 个槽位费用 = alpha * k（已占用 k = 锁定数 + 1 起算），
  从而把“评审负载偏差”做成凸的边际成本，流会天然趋向均衡
* M_i -> R_j 边费用 = beta * 该评审未覆盖的稿件主题数

锁定的既有分配不进入网络，直接预占需求与容量并计入负载和费用。
冲突边根本不会被加入图中，因此绝不可能被使用。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from .db import get_setting, set_setting, transaction
from .mcmf import MinCostMaxFlow


def _rows(conn: sqlite3.Connection, sql: str) -> list[sqlite3.Row]:
    return conn.execute(sql).fetchall()


def solve(conn: sqlite3.Connection) -> dict:
    beta = int(get_setting(conn, "mismatch_weight"))   # 主题不匹配权重
    alpha = int(get_setting(conn, "load_weight"))      # 负载偏差权重

    manuscripts = [dict(r) for r in _rows(
        conn, "SELECT * FROM manuscripts ORDER BY id")]
    reviewers = [dict(r) for r in _rows(
        conn, "SELECT * FROM reviewers ORDER BY id")]

    conflict_pairs: set[tuple[int, int]] = {
        (r["m_id"], r["r_id"])
        for r in _rows(conn, "SELECT m_id, r_id FROM conflicts")
    }
    lock_rows = _rows(conn, "SELECT m_id, r_id FROM assignments WHERE locked = 1 ORDER BY m_id, r_id")

    m_map = {m["id"]: m for m in manuscripts}
    r_map = {r["id"]: r for r in reviewers}
    m_by_id = {m["id"]: {**m, "topics": json.loads(m["topics"])} for m in manuscripts}
    r_by_id = {r["id"]: {**r, "topics": json.loads(r["topics"])} for r in reviewers}

    warnings: list[dict] = []

    # ---------------------------------------------------------------- 锁定校验
    # 有效锁：稿件/评审人均存在、无冲突、且未超过该稿所需评审数。
    # 无效锁保留在表中（由用户解锁），但不参与本次求解。
    locks_by_m: dict[int, list[int]] = {}
    raw_pairs: list[tuple[int, int]] = []
    for row in lock_rows:
        m_id, r_id = row["m_id"], row["r_id"]
        raw_pairs.append((m_id, r_id))
        if m_id not in m_map or r_id not in r_map:
            warnings.append({
                "code": "lock_missing_entity",
                "message": f"锁定分配（稿件 #{m_id} / 评审人 #{r_id}）指向已删除对象，已忽略。",
                "manuscript_id": m_id, "reviewer_id": r_id,
            })
            continue
        if (m_id, r_id) in conflict_pairs:
            warnings.append({
                "code": "lock_conflict",
                "message": f"《{m_map[m_id]['title']}》与 {r_map[r_id]['name']} 存在作者冲突，该锁定被忽略，请先解除冲突或解锁。",
                "manuscript_id": m_id, "reviewer_id": r_id,
            })
            continue
        locks_by_m.setdefault(m_id, []).append(r_id)

    valid_locks: list[tuple[int, int]] = []
    for m_id, r_ids in locks_by_m.items():
        req = m_map[m_id]["required_reviews"]
        if len(r_ids) > req:
            for extra in r_ids[req:]:
                warnings.append({
                    "code": "lock_exceeds_required",
                    "message": f"《{m_map[m_id]['title']}》锁定了 {len(r_ids)} 人，超过每稿 {req} 人的需求，"
                               f"超出的锁定（{r_map[extra]['name']}）本次不计入。",
                    "manuscript_id": m_id, "reviewer_id": extra,
                })
        valid_locks.extend((m_id, r_id) for r_id in r_ids[:req])

    locked_pairs: set[tuple[int, int]] = set(valid_locks)
    locked_count: dict[int, int] = {}
    for _, r_id in valid_locks:
        locked_count[r_id] = locked_count.get(r_id, 0) + 1

    # 锁定数超过个人容量：强制保留（既有分配），剩余容量为 0，并给出警告。
    free_capacity: dict[int, int] = {}
    for r in reviewers:
        cap, lc = r["capacity"], locked_count.get(r["id"], 0)
        if lc > cap:
            warnings.append({
                "code": "reviewer_over_capacity",
                "message": f"{r['name']} 已有 {lc} 个锁定分配，超过个人容量 {cap}；"
                           f"锁定分配强制保留，本次不再向其安排新稿。",
                "reviewer_id": r["id"],
            })
            free_capacity[r["id"]] = 0
        else:
            free_capacity[r["id"]] = cap - lc

    # ------------------------------------------------- 锁定部分的费用（确定性）
    def uncovered(m_id: int, r_id: int) -> int:
        return len(set(m_by_id[m_id]["topics"]) - set(r_by_id[r_id]["topics"]))

    edge_costs: dict[str, dict] = {}
    locked_cost = 0
    lock_slot_seen: dict[int, int] = {}
    for m_id, r_id in valid_locks:
        k = lock_slot_seen.get(r_id, 0) + 1
        lock_slot_seen[r_id] = k
        mm = beta * uncovered(m_id, r_id)
        ll = alpha * k
        locked_cost += mm + ll
        edge_costs[f"{m_id}:{r_id}"] = {
            "mismatch_cost": mm, "load_cost": ll,
            "covered": sorted(set(m_by_id[m_id]["topics"]) & set(r_by_id[r_id]["topics"])),
            "uncovered_topics": sorted(set(m_by_id[m_id]["topics"]) - set(r_by_id[r_id]["topics"])),
        }

    locked_by_m_count: dict[int, int] = {}
    for m_id, _ in valid_locks:
        locked_by_m_count[m_id] = locked_by_m_count.get(m_id, 0) + 1

    # ------------------------------------------------------------ 构造流网络
    S = 0
    m_node = {m["id"]: 1 + i for i, m in enumerate(manuscripts)}
    r_base = 1 + len(manuscripts)
    r_node = {r["id"]: r_base + i for i, r in enumerate(reviewers)}
    T = r_base + len(reviewers)
    net = MinCostMaxFlow(T + 1)

    free_need: dict[int, int] = {}
    for m in manuscripts:
        need = max(0, m["required_reviews"] - locked_by_m_count.get(m["id"], 0))
        free_need[m["id"]] = need
        if need:
            net.add_edge(S, m_node[m["id"]], need, 0, {"type": "source", "m_id": m["id"], "cap": need})

    for m in manuscripts:
        for r in reviewers:
            m_id, r_id = m["id"], r["id"]
            # 冲突边绝不加入；锁定边已预占，也不重复建立
            if (m_id, r_id) in conflict_pairs or (m_id, r_id) in locked_pairs:
                continue
            net.add_edge(
                m_node[m_id], r_node[r_id], 1,
                beta * uncovered(m_id, r_id),
                {"type": "assign", "m_id": m_id, "r_id": r_id, "cap": 1},
            )

    for r in reviewers:
        r_id = r["id"]
        lc = locked_count.get(r_id, 0)
        for k in range(1, free_capacity[r_id] + 1):
            slot = lc + k  # 全局负载槽位序号，与锁定部分连续计费
            net.add_edge(
                r_node[r_id], T, 1, alpha * slot,
                {"type": "slot", "r_id": r_id, "slot": slot, "cap": 1},
            )

    # ------------------------------------------------------------- 求解与落库
    flow, free_cost = net.solve(S, T)

    free_pairs: list[tuple[int, int]] = []
    for tag, used in net.used_tags():
        if tag["type"] == "assign" and used > 0:
            free_pairs.append((tag["m_id"], tag["r_id"]))
    free_pairs.sort()

    # 自由分配边的费用拆解（槽位按评审人分组后顺序对应）
    assigned_slot: dict[int, list[int]] = {}
    for tag, used in net.used_tags():
        if tag["type"] == "slot" and used > 0:
            assigned_slot.setdefault(tag["r_id"], []).append(tag["slot"])
    ms_by_reviewer: dict[int, list[int]] = {}
    for m_id, r_id in free_pairs:
        ms_by_reviewer.setdefault(r_id, []).append(m_id)
    for r_id, ms in ms_by_reviewer.items():
        for idx, m_id in enumerate(sorted(ms)):
            slots = sorted(assigned_slot.get(r_id, []))
            slot = slots[idx] if idx < len(slots) else (locked_count.get(r_id, 0) + idx + 1)
            mm = beta * uncovered(m_id, r_id)
            ll = alpha * slot
            edge_costs[f"{m_id}:{r_id}"] = {
                "mismatch_cost": mm, "load_cost": ll,
                "covered": sorted(set(m_by_id[m_id]["topics"]) & set(r_by_id[r_id]["topics"])),
                "uncovered_topics": sorted(set(m_by_id[m_id]["topics"]) - set(r_by_id[r_id]["topics"])),
            }

    # 事务内用新结果替换所有非锁定分配
    with transaction(conn):
        conn.execute("DELETE FROM assignments WHERE locked = 0")
        for m_id, r_id in free_pairs:
            conn.execute(
                "INSERT INTO assignments(m_id, r_id, locked) VALUES (?, ?, 0)",
                (m_id, r_id),
            )

    # --------------------------------------------------------- 未满足稿件分析
    assigned_by_m: dict[int, int] = {}
    for m_id, _ in valid_locks:
        assigned_by_m[m_id] = assigned_by_m.get(m_id, 0) + 1
    for m_id, _ in free_pairs:
        assigned_by_m[m_id] = assigned_by_m.get(m_id, 0) + 1

    total_capacity = sum(r["capacity"] for r in reviewers)
    total_required = sum(m["required_reviews"] for m in manuscripts)

    unmet: list[dict] = []
    for m in manuscripts:
        m_id = m["id"]
        got = assigned_by_m.get(m_id, 0)
        deficit = m["required_reviews"] - got
        if deficit <= 0:
            continue

        reasons: list[str] = []
        candidates = []
        blocked_by_conflict = 0
        candidate_capacity = 0
        for r in reviewers:
            r_id = r["id"]
            if (m_id, r_id) in conflict_pairs:
                blocked_by_conflict += 1
                continue
            if (m_id, r_id) in locked_pairs:
                continue  # 已计入 got
            rem = free_capacity[r_id]
            miss = uncovered(m_id, r_id)
            candidate_capacity += rem
            candidates.append({
                "reviewer_id": r_id, "name": r["name"],
                "remaining_capacity": rem,
                "uncovered_topics": miss,
                "topic_match": "full" if miss == 0 else "partial",
            })

        if blocked_by_conflict:
            reasons.append(f"{len(reviewers)} 名评审人中有 {blocked_by_conflict} 名因作者冲突被禁止评审本稿。")
        # 注意：主题不匹配只是高费用（软约束），不禁止分配；
        # 这里分层展示，便于人工复核未满足的真正原因。
        compatible_cap = sum(c["remaining_capacity"] for c in candidates
                             if c["topic_match"] == "full")
        compatible_n = sum(1 for c in candidates if c["topic_match"] == "full")
        eligible = [c for c in candidates if c["remaining_capacity"] > 0]
        if not eligible:
            reasons.append("其余评审人剩余容量均为 0（个人容量已被锁定或其他稿件占满）。")
        elif candidate_capacity < deficit:
            reasons.append(
                f"无冲突评审人的剩余总容量为 {candidate_capacity}，小于本稿缺口 {deficit}；"
                f"其中主题完全相容者 {compatible_n} 名、剩余容量合计 {compatible_cap}。"
                f"可在「评审人」中提高容量或解除相关冲突。"
            )
        else:
            reasons.append(
                f"无冲突评审人剩余总容量为 {candidate_capacity}（不小于缺口 {deficit}），"
                f"但这些容量与其他稿件共享（每稿-每人最多一次），在全局最小费用竞争下本稿仍缺 {deficit}；"
                f"主题完全相容者 {compatible_n} 名、剩余容量合计 {compatible_cap}，"
                f"可考虑增加方向匹配的评审人或提高其容量。"
            )
        if total_capacity < total_required:
            reasons.append(
                f"全局性容量不足：全部评审人容量之和 {total_capacity} < 全部稿件需求之和 {total_required}。"
            )

        unmet.append({
            "manuscript_id": m_id,
            "title": m["title"],
            "required": m["required_reviews"],
            "assigned": got,
            "deficit": deficit,
            "reasons": reasons,
            "details": {
                "reviewer_count": len(reviewers),
                "blocked_by_conflict": blocked_by_conflict,
                "candidate_remaining_capacity": candidate_capacity,
                "candidates": candidates,
            },
        })

    solution = {
        "solved_at": datetime.now().isoformat(timespec="seconds"),
        "mismatch_weight": beta,
        "load_weight": alpha,
        "total_required": total_required,
        "total_assigned": len(valid_locks) + len(free_pairs),
        "total_capacity": total_capacity,
        "locked_count": len(valid_locks),
        "total_cost": locked_cost + free_cost,
        "locked_cost": locked_cost,
        "free_cost": free_cost,
        "fully_satisfied": len(unmet) == 0,
        "unmet": unmet,
        "warnings": warnings,
        "edge_costs": edge_costs,
    }

    set_setting(conn, "last_solution", solution)
    conn.commit()
    return solution
