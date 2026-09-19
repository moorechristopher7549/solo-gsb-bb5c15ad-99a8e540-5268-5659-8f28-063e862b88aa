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
    max_same_inst = int(get_setting(conn, "max_same_institution"))  # 每稿同机构上限；0=关闭

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

    # ------------------------------------------------ 机构多样性：锁定占用名额
    # 无机构（institution 为 None / 空串）的评审人不计入机构名额。
    # 历史锁定已超过机构上限：锁定强制保留（既有分配），发出警告，
    # 本次求解不再向该稿件-机构安排新评审人（在下方建边时跳过）。
    inst_locked: dict[tuple[int, str], int] = {}
    for m_id, r_id in valid_locks:
        inst = r_map[r_id].get("institution")
        if inst:
            inst_locked[(m_id, inst)] = inst_locked.get((m_id, inst), 0) + 1

    inst_blocked: set[tuple[int, str]] = set()
    if max_same_inst > 0:
        for (m_id, inst), cnt in inst_locked.items():
            if cnt > max_same_inst:
                inst_blocked.add((m_id, inst))
                warnings.append({
                    "code": "institution_lock_exceeds_limit",
                    "message": f"《{m_map[m_id]['title']}》来自 {inst} 的锁定评审有 {cnt} 人，"
                               f"已超过每稿同机构 {max_same_inst} 人的上限；"
                               f"锁定分配强制保留，本次不再向该机构安排新评审人。",
                    "manuscript_id": m_id, "institution": inst,
                    "locked_count": cnt, "limit": max_same_inst,
                })

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
    inst_base = r_base + len(reviewers)

    free_need: dict[int, int] = {}
    for m in manuscripts:
        free_need[m["id"]] = max(
            0, m["required_reviews"] - locked_by_m_count.get(m["id"], 0))

    # 机构多样性：为每个“稿件×机构”建立一个中转节点，
    # 进入该节点的总容量 = 同机构剩余名额（上限 − 已锁定数），硬性限制同机构人数。
    # 历史锁定已超限的（稿件, 机构）不建机构节点 -> 该机构评审人对本稿不可达。
    inst_node: dict[tuple[int, str], int] = {}
    if max_same_inst > 0:
        for m in manuscripts:
            m_id = m["id"]
            for r in reviewers:
                inst = r["institution"]
                if not inst or (m_id, inst) in inst_node or (m_id, inst) in inst_blocked:
                    continue
                inst_node[(m_id, inst)] = inst_base + len(inst_node)

    T = inst_base + len(inst_node)
    net = MinCostMaxFlow(T + 1)

    for m in manuscripts:
        need = free_need[m["id"]]
        if need:
            net.add_edge(S, m_node[m["id"]], need, 0, {"type": "source", "m_id": m["id"], "cap": need})

    if max_same_inst > 0:
        for (m_id, inst), node in inst_node.items():
            remaining = max(0, max_same_inst - inst_locked.get((m_id, inst), 0))
            net.add_edge(
                m_node[m_id], node, remaining, 0,
                {"type": "institution", "m_id": m_id, "institution": inst, "cap": remaining},
            )

    for m in manuscripts:
        for r in reviewers:
            m_id, r_id = m["id"], r["id"]
            # 冲突边绝不加入；锁定边已预占，也不重复建立
            if (m_id, r_id) in conflict_pairs or (m_id, r_id) in locked_pairs:
                continue
            cost = beta * uncovered(m_id, r_id)
            tag = {"type": "assign", "m_id": m_id, "r_id": r_id, "cap": 1}
            if max_same_inst > 0 and r["institution"]:
                node = inst_node.get((m_id, r["institution"]))
                # 该（稿件, 机构）名额已被历史锁定用尽：不再向此机构分配
                if node is None:
                    continue
                net.add_edge(node, r_node[r_id], 1, cost, tag)
            else:
                # 关闭机构约束或评审人无机构：无机构者不计入机构上限，直接 M -> R
                net.add_edge(m_node[m_id], r_node[r_id], 1, cost, tag)

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

    # 每篇稿件最终（锁定 + 新分配）的机构占用构成：无机构者计入 None 组，不计名额
    inst_groups: dict[int, dict[str | None, dict]] = {}

    def _bump_inst(m_id: int, r_id: int, locked: bool) -> None:
        inst = r_map[r_id].get("institution")
        groups = inst_groups.setdefault(m_id, {})
        g = groups.setdefault(inst, {
            "institution": inst, "count": 0, "locked_count": 0,
            "reviewers": [], "at_limit": False, "over_limit": False,
        })
        g["count"] += 1
        g["locked_count"] += 1 if locked else 0
        g["reviewers"].append(r_map[r_id]["name"])

    for m_id, r_id in valid_locks:
        _bump_inst(m_id, r_id, True)
    for m_id, r_id in free_pairs:
        _bump_inst(m_id, r_id, False)

    for m_id, groups in inst_groups.items():
        for inst, g in groups.items():
            if not inst or max_same_inst <= 0:
                continue
            g["at_limit"] = g["count"] == max_same_inst
            g["over_limit"] = g["count"] > max_same_inst

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
        blocked_by_institution = 0
        candidate_capacity = 0
        inst_blocked_names: set[str] = set()
        for r in reviewers:
            r_id = r["id"]
            if (m_id, r_id) in conflict_pairs:
                blocked_by_conflict += 1
                continue
            if (m_id, r_id) in locked_pairs:
                continue  # 已计入 got
            rem = free_capacity[r_id]
            miss = uncovered(m_id, r_id)
            inst = r["institution"]
            # 机构名额：已被锁定（及本次竞争）占满的机构候选不可再分配
            used = inst_locked.get((m_id, inst), 0) if inst else 0
            inst_full = (
                max_same_inst > 0 and bool(inst)
                and used >= max_same_inst
            )
            candidates.append({
                "reviewer_id": r_id, "name": r["name"],
                "institution": inst,
                "remaining_capacity": rem,
                "uncovered_topics": miss,
                "topic_match": "full" if miss == 0 else "partial",
                "institution_blocked": inst_full,
            })
            if inst_full:
                blocked_by_institution += 1
                inst_blocked_names.add(inst)
            else:
                candidate_capacity += rem

        if blocked_by_conflict:
            reasons.append(f"{len(reviewers)} 名评审人中有 {blocked_by_conflict} 名因作者冲突被禁止评审本稿。")
        if blocked_by_institution:
            reasons.append(
                f"{blocked_by_institution} 名无冲突候选来自机构名额已满的机构（每稿同机构最多 "
                f"{max_same_inst} 人，锁定分配先占用名额）："
                f"{'、'.join(sorted(inst_blocked_names))}；这些候选被机构上限排除，不再计入可用容量。"
            )
        # 注意：主题不匹配只是高费用（软约束），不禁止分配；
        # 这里分层展示，便于人工复核未满足的真正原因。
        open_candidates = [c for c in candidates if not c["institution_blocked"]]
        compatible_cap = sum(c["remaining_capacity"] for c in open_candidates
                             if c["topic_match"] == "full")
        compatible_n = sum(1 for c in open_candidates if c["topic_match"] == "full")
        eligible = [c for c in open_candidates if c["remaining_capacity"] > 0]
        if not candidates:
            reasons.append("除冲突与已锁定评审外没有其他候选评审人。")
        elif not eligible:
            reasons.append("其余可参评评审人剩余容量均为 0（个人容量已被锁定或其他稿件占满）。")
        elif candidate_capacity < deficit:
            reasons.append(
                f"无冲突且未被机构上限排除的评审人剩余总容量为 {candidate_capacity}，小于本稿缺口 {deficit}；"
                f"其中主题完全相容者 {compatible_n} 名、剩余容量合计 {compatible_cap}。"
                f"可在「评审人」中提高容量、解除相关冲突或放宽同机构人数上限。"
            )
        else:
            reasons.append(
                f"可用评审人剩余总容量为 {candidate_capacity}（不小于缺口 {deficit}），"
                f"但这些容量与其他稿件共享（每稿-每人最多一次、同机构有名额限制），"
                f"在全局最小费用竞争下本稿仍缺 {deficit}；"
                f"主题完全相容者 {compatible_n} 名、剩余容量合计 {compatible_cap}，"
                f"可考虑增加方向匹配的评审人、提高其容量或放宽同机构人数上限。"
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
                "blocked_by_institution": blocked_by_institution,
                "candidate_remaining_capacity": candidate_capacity,
                "candidates": candidates,
            },
        })

    institution_composition: dict[str, list[dict]] = {}
    for m in manuscripts:
        groups = inst_groups.get(m["id"], {})
        ordered = sorted(
            groups.values(),
            key=lambda g: (g["institution"] is None, g["institution"] or ""),
        )
        institution_composition[str(m["id"])] = ordered

    solution = {
        "solved_at": datetime.now().isoformat(timespec="seconds"),
        "mismatch_weight": beta,
        "load_weight": alpha,
        "max_same_institution": max_same_inst,
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
        "institution_composition": institution_composition,
    }

    set_setting(conn, "last_solution", solution)
    conn.commit()
    return solution


def precheck_new_lock(conn: sqlite3.Connection, m_id: int, r_id: int) -> None:
    """新增/转为锁定前的硬性校验；不通过时抛 ValueError（路由层转为 409）。

    * 锁定不得使该稿人数超过每稿所需评审数（已存在的行转锁定不新增人数，不检查）；
    * 开启机构上限时，新增锁定不得使该稿同机构锁定数超过上限——
      这与“历史锁定已超限只警告”的规则区分开：历史锁保留，新锁必须被拒绝。
    """
    m_row = conn.execute(
        "SELECT required_reviews FROM manuscripts WHERE id = ?", (m_id,)
    ).fetchone()
    if m_row is None:
        raise ValueError("稿件不存在")
    req = m_row["required_reviews"]

    existing = conn.execute(
        "SELECT locked FROM assignments WHERE m_id = ? AND r_id = ?",
        (m_id, r_id),
    ).fetchone()
    if existing is not None and existing["locked"]:
        return  # 已锁定的行重复提交：幂等，无需再次占用名额

    locked_n = conn.execute(
        "SELECT COUNT(*) AS c FROM assignments WHERE m_id = ? AND locked = 1",
        (m_id,),
    ).fetchone()["c"]
    if existing is None and locked_n >= req:
        raise ValueError(
            f"该稿件最多需要 {req} 名评审，已锁定 {locked_n} 人，"
            f"再锁定一名新评审人将超出需求；请先解锁一把锁。"
        )

    limit = int(get_setting(conn, "max_same_institution"))
    if limit > 0:
        inst_row = conn.execute(
            "SELECT institution FROM reviewers WHERE id = ?", (r_id,)
        ).fetchone()
        if inst_row is None:
            raise ValueError("评审人不存在")
        institution = inst_row["institution"]
        if institution:
            same_inst = conn.execute(
                "SELECT COUNT(*) AS c FROM assignments a JOIN reviewers rv ON a.r_id = rv.id "
                "WHERE a.m_id = ? AND a.locked = 1 AND rv.institution = ?",
                (m_id, institution),
            ).fetchone()["c"]
            if same_inst >= limit:
                raise ValueError(
                    f"《{conn.execute('SELECT title FROM manuscripts WHERE id = ?', (m_id,)).fetchone()['title']}》"
                    f"来自 {institution} 的锁定评审已有 {same_inst} 人，"
                    f"达到每稿同机构最多 {limit} 人的上限；再锁定该机构评审人将超限，请先解锁一把锁或调整设置。"
                )
