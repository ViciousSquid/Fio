"""Linked portals share one active state.

Two portals are linked when either names the other as its ``portal_target``,
and links chain: A -> B and B -> C put all three in one group. A portal's
group is either all active or all inactive -- never half of each, which drew
a view through a portal that could not be walked through (a transit needs
both ends active).

Everything that sets ``active`` goes through :func:`set_group_active`, and
everything that links two portals through :func:`sync_group_to`. A level whose
portals disagree (authored before this rule, or edited by hand) is put right
by :func:`normalize_portal_groups` as it loads: a group with any inactive
portal becomes wholly inactive, which is how it already played, since no
transit could happen through it.
"""

from __future__ import annotations

from engine.change_journal import touch


def _is_portal(thing):
    from editor.things import Portal
    return isinstance(thing, Portal)


def portal_group(portal, things):
    """*portal* and every portal linked to it, directly or through others,
    in *things* order."""
    portals = [t for t in things if _is_portal(t)]
    by_name = {}
    for p in portals:
        name = p.properties.get('name')
        if name:
            by_name.setdefault(name, []).append(p)
    # Links are undirected: A targets B, or B targets A.
    neighbours = {id(p): [] for p in portals}
    for p in portals:
        for other in by_name.get(p.properties.get('portal_target') or '', ()):
            if other is not p:
                neighbours[id(p)].append(other)
                neighbours[id(other)].append(p)
    seen = {id(portal)}
    stack = [portal]
    while stack:
        for other in neighbours.get(id(stack.pop()), ()):
            if id(other) not in seen:
                seen.add(id(other))
                stack.append(other)
    return [p for p in portals if id(p) in seen] or [portal]


def _apply(portal, active):
    portal.properties['active'] = active
    if hasattr(portal, '_fade_target'):
        portal._fade_target = 1.0 if active else 0.0
    touch(portal)


def set_group_active(portal, things, active):
    """Set *portal* and every portal linked to it to *active*.

    Returns the linked portals whose state this changed (never *portal*
    itself, which is always written).
    """
    active = bool(active)
    _apply(portal, active)
    changed = []
    for other in portal_group(portal, things):
        if other is not portal and other.is_active() != active:
            _apply(other, active)
            changed.append(other)
    return changed


def sync_group_to(portal, things):
    """Bring every portal linked to *portal* to *portal*'s state.

    For a new or changed link: the portal being edited sets the group.
    """
    return set_group_active(portal, things, portal.is_active())


def normalize_portal_groups(things):
    """Put right every group whose portals disagree: any inactive portal makes
    the whole group inactive. Returns the portals changed."""
    changed = []
    done = set()
    for thing in things:
        if not _is_portal(thing) or id(thing) in done:
            continue
        group = portal_group(thing, things)
        done.update(id(p) for p in group)
        if len({p.is_active() for p in group}) > 1:
            for p in group:
                if p.is_active():
                    _apply(p, False)
                    changed.append(p)
    return changed
