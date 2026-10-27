"""The initiative total: one cost across a follow-up lineage (C1).

An initiative is a follow-up *connected component*: every task joined to the
others by ``follow_up_of`` links, in either direction (links never cross
plans, ``node_creation``). Its cost is each node of one set summed once on its
*own* cost, never the members' rolled costs, because a follow-up of a sub-step
sits inside another member's subtree and summing rolled costs would count it
twice. The set is

* every member, and every node beneath a member (archived included);
* the delegating step a member was re-attached under (see the limit below);
* every node whose ``commitment_id`` is a member's *own id*, wherever its
  plan sits: a member that is a commitment root is where the obligation
  started, so what shares it (split siblings, re-attached delegated nodes)
  belongs to the same initiative; and
* every plan attached beneath a member or a node beneath one, with all of that
  plan's nodes and the plans attached beneath them (``node_cost`` rolls the
  same plans).

A node is never added merely because it shares an *ancestor's* commitment:
descendants inherit their parent's ``commitment_id``, so a member nested under
a phase would otherwise pull in every unrelated sibling of the phase. Limit:
a split sibling of a member that is not itself a commitment root has no
canonical link of its own to find it by, so it is left out; the total
undercounts there rather than overcounting. A member whose plan is attached
beneath its commitment root is a re-attached delegated node: that root (the
delegating step) is the canonical link, so its own cost is counted; any other
root, such as a phase, is an ancestor and never is.

Only a member and the nodes beneath it open the plans attached under them; a
node counted for its commitment adds its own cost alone, never its attached plan.

That set contains each member's rolled cost, so the total is never below a
member's Tokens line; for a member that is a commitment root it also contains
every commitment sharer, so it is never below that commitment total's nodes.
``node_cost`` owns the call; this module
holds the membership and the sum, and reads only rows ``node_cost`` already
loaded plus the sharers and re-attached roots outside them, by id and by
commitment, never the plans they sit in. ``expand`` reads no cost: the caller
reads every row's own cost, outside rows included, in the one priced read it
makes for the plans, and ``totals`` sums them.
"""

from __future__ import annotations

from collections import defaultdict

from plans import hierarchy, node_cost
from plans.node_states import _TERMINAL_STATES


def components(nodes: list[dict]) -> list[list[str]]:
    """Follow-up connected components of two or more nodes, as id lists."""
    ids = {node["id"] for node in nodes}
    parent = {node_id: node_id for node_id in ids}

    def find(node_id: str) -> str:
        while parent[node_id] != node_id:
            parent[node_id] = parent[parent[node_id]]
            node_id = parent[node_id]
        return node_id

    for node in nodes:
        link = node["follow_up_of"]
        if link is not None and link in ids:
            parent[find(node["id"])] = find(link)
    groups: dict[str, list[str]] = defaultdict(list)
    for node_id in ids:
        groups[find(node_id)].append(node_id)
    return [sorted(group) for group in groups.values() if len(group) > 1]


def node_set(
    members: list[str], *, by_id: dict, children: dict, by_commitment: dict,
    plan_nodes: dict, attached: dict,
) -> set[str]:
    """Every node whose own cost the initiative sums, each once."""
    found: set[str] = set()
    descended: set[str] = set()
    expanded: set[str] = set()
    # (node, descend into its children, expand the plan attached beneath it).
    # Only a member and what lies beneath it open their attached plans; a node
    # added for its commitment counts its own cost and nothing under it.
    stack: list[tuple[str, bool, bool]] = [(member, True, True) for member in members]
    for member in members:
        root = str(by_id[member]["commitment_id"])
        if root == member:
            stack.extend((other, False, False) for other in by_commitment.get(member, []))
        elif root in by_id and attached.get(root) == by_id[member]["plan_id"]:
            stack.append((root, False, False))
    while stack:
        node_id, descend, expand = stack.pop()
        if descend and node_id not in descended:
            descended.add(node_id)
            stack.extend((child, True, True) for child in children.get(node_id, []))
        if expand and node_id not in expanded:
            expanded.add(node_id)
            child_plan = attached.get(node_id)
            if child_plan is not None:
                stack.extend((inner, False, True) for inner in plan_nodes.get(child_plan, []))
        found.add(node_id)
    return found


def is_open(node: dict) -> bool:
    """A member still to finish: not settled and not archived."""
    return node["archived_at"] is None and node["state"] not in _TERMINAL_STATES


#: ``expand``'s answer: the lineages, and the rows and attachments their totals read.
Expanded = tuple[list[list[str]], dict[str, list[dict]], dict[str, str]]


def expand(connection, nodes_by_plan: dict[str, list[dict]], attached: dict[str, str]) -> Expanded | None:
    """The follow-up lineages of these loaded plans, with the outside rows their totals sum.

    None when the plans hold no lineage. Every node id in the returned rows needs its own
    cost for ``totals``.
    """
    lineages = [
        group for rows in nodes_by_plan.values() for group in components(rows)
    ]
    if not lineages:
        return None
    return (lineages, *_with_sharers(connection, lineages, nodes_by_plan, attached))


def node_ids(expanded: Expanded) -> list[str]:
    """Every node whose own cost ``totals`` may sum."""
    return [str(row["id"]) for rows in expanded[1].values() for row in rows]


def totals(expanded: Expanded, own: dict[str, dict]) -> dict[str, dict]:
    """Each follow-up lineage member's initiative total, keyed by node id."""
    lineages, nodes_by_plan, attached = expanded
    all_nodes = [row for rows in nodes_by_plan.values() for row in rows]
    by_id = {row["id"]: row for row in all_nodes}
    children: dict[str, list[str]] = defaultdict(list)
    by_commitment: dict[str, list[str]] = defaultdict(list)
    for row in all_nodes:
        if row["parent_id"] in by_id:
            children[row["parent_id"]].append(row["id"])
        by_commitment[str(row["commitment_id"])].append(row["id"])
    plan_nodes = {plan_id: [row["id"] for row in rows] for plan_id, rows in nodes_by_plan.items()}
    found: dict[str, dict] = {}
    for members in lineages:
        summed = node_set(
            members, by_id=by_id, children=children, by_commitment=by_commitment,
            plan_nodes=plan_nodes, attached=attached,
        )
        total = node_cost._empty_own()
        for node_id in summed:
            total = node_cost._add(total, own[node_id])
        value = {
            "follow_ups": len(members) - 1,
            "cost": node_cost._settled(total),
            "open": any(is_open(by_id[member]) for member in members),
        }
        found.update((member, value) for member in members)
    return found


def _with_sharers(connection, lineages, nodes_by_plan, attached) -> tuple[dict, dict]:
    """Add the nodes ``node_set`` sums from outside the loaded plans.

    A sharer can sit above the projected plans (a delegating step in the
    parent plan), so the total must not depend on which plans were asked for.
    ``node_set`` sums two kinds of outside node, each for its own cost alone and
    never opening its subtree or its attached plan: the nodes sharing the
    commitment of a member that is its own commitment root, and the root a
    member was re-attached under. So only those rows are read, by commitment
    and by id, with the one attachment the re-attached
    root's test needs -- not the plans they sit in, which hold a history that
    grows without bound.
    """
    by_id = {row["id"]: row for rows in nodes_by_plan.values() for row in rows}
    members = sorted({member for group in lineages for member in group})
    own_roots = [m for m in members if str(by_id[m]["commitment_id"]) == m]
    above = sorted(
        {str(by_id[m]["commitment_id"]) for m in members} - set(own_roots) - set(by_id)
    )
    shared = node_cost._rows_in(
        connection,
        f"SELECT {node_cost.NODE_COLUMNS} FROM plan_nodes WHERE commitment_id IN ({{marks}})",
        own_roots,
    )
    roots = node_cost._rows_in(
        connection,
        f"SELECT {node_cost.NODE_COLUMNS} FROM plan_nodes WHERE id IN ({{marks}})",
        above,
    )
    outside = {
        str(row["id"]): dict(row) for row in [*shared, *roots] if row["id"] not in by_id
    }
    if not outside:
        return nodes_by_plan, attached
    # An outside row can sit in a loaded plan (a scoped read loads only the members'
    # subtrees), so it joins that plan's rows rather than replacing them.
    merged: dict[str, list[dict]] = {plan_id: list(rows) for plan_id, rows in nodes_by_plan.items()}
    for row in outside.values():
        merged.setdefault(str(row["plan_id"]), []).append(row)
    outside_roots = {str(row["id"]): outside[str(row["id"])] for row in roots if str(row["id"]) in outside}
    more_attached = _reattachments(connection, [by_id[m] for m in members], outside_roots)
    return merged, {**attached, **more_attached}


def _reattachments(connection, members: list[dict], roots: dict[str, dict]) -> dict[str, str]:
    """Map each outside root to the member plan it carries, where it is the carrier.

    ``node_set`` counts the root a member was re-attached under only when that
    root is the node showing the member's plan (``hierarchy.attaching_nodes``),
    so this asks that one question for the member's plan and no other.
    """
    found: dict[str, str] = {}
    asked: set[tuple[str, str]] = set()
    for member in members:
        root = roots.get(str(member["commitment_id"]))
        if root is None:
            continue
        plan_id = str(member["plan_id"])
        # Members of one plan share the answer: ask once per plan and root.
        if (plan_id, str(root["id"])) in asked:
            continue
        asked.add((plan_id, str(root["id"])))
        owner = connection.execute("SELECT owner_id FROM plans WHERE id=?", (plan_id,)).fetchone()
        if owner is None:
            continue
        owners = hierarchy.attaching_nodes(
            connection, [str(root["plan_id"])], worker=str(owner["owner_id"]),
        )
        if owners.get(plan_id) == str(root["id"]):
            found[str(root["id"])] = plan_id
    return found
