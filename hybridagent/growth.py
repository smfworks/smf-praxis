"""Growth surfaces — skill inbox, evolution proposals, user-model summary.

Evolution proposals are durable when a Store is bound (survive daemon restart).
Apply reconstructs a :class:`~hybridagent.evolution.Proposal` from stored fields.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import config as cfg
from .logging_util import get_logger

if TYPE_CHECKING:
    from .persistence import Store

_log = get_logger("praxis.growth")

# In-memory fallback when no store is bound (tests).
_PROPOSALS: list[dict] = []
_PROP_STORE: "Store | None" = None


def set_proposal_store(store: "Store | None") -> None:
    global _PROP_STORE
    _PROP_STORE = store


def proposal_store() -> "Store | None":
    return _PROP_STORE


def list_skills(agent) -> list[dict]:
    out: list[dict] = []
    lib = getattr(agent, "skills", None) if agent else None
    if lib is None:
        try:
            from .persistence import Store
            from .skills import SkillLibrary
            lib = SkillLibrary(store=Store.open())
        except Exception:
            return out
    try:
        skills = lib.list() if hasattr(lib, "list") else []
    except Exception:
        skills = []
    for s in skills or []:
        name = getattr(s, "name", None) or (s.get("name") if isinstance(s, dict) else None)
        if not name:
            continue
        out.append({
            "name": name,
            "trigger": getattr(s, "trigger", "") if not isinstance(s, dict)
            else s.get("trigger", ""),
            "enabled": getattr(s, "enabled", True) if not isinstance(s, dict)
            else s.get("enabled", True),
            "body_preview": (
                (getattr(s, "body", "") if not isinstance(s, dict) else s.get("body", ""))
                or "")[:240],
        })
    return out


def _proposal_id(skill_name: str, new_fitness: float) -> str:
    return f"evo-{skill_name}-{int(float(new_fitness) * 10000)}"


def _row_public(row: dict) -> dict:
    """Strip internal keys for API responses."""
    return {
        "id": row.get("id") or row.get("proposal_id"),
        "skill_name": row.get("skill_name"),
        "current_trigger": row.get("current_trigger", ""),
        "new_trigger": row.get("new_trigger", ""),
        "current_fitness": row.get("current_fitness", 0),
        "new_fitness": row.get("new_fitness", 0),
        "improves": bool(row.get("improves")),
        "rationale": row.get("rationale", ""),
        "diff": row.get("diff") or row.get("diff_text") or "",
        "source": row.get("source", ""),
        "status": row.get("status", "pending"),
    }


def run_evolve(agent, llm=None, limit: int = 3,
               store: "Store | None" = None) -> list[dict]:
    """Propose skill improvements; persist for human apply/reject."""
    from . import evolution as evo
    from .skills import SkillLibrary
    st = store if store is not None else _PROP_STORE
    lib = getattr(agent, "skills", None)
    if lib is None:
        from .persistence import Store
        lib = SkillLibrary(store=st or Store.open())
    names = [s["name"] for s in list_skills(agent)][: max(1, min(limit, 10))]
    public: list[dict] = []
    for name in names:
        try:
            prop = evo.evolve_skill(lib, name, llm=llm)
        except Exception as exc:  # noqa: BLE001
            _log.info("evolve %s failed: %s", name, exc)
            continue
        if prop is None:
            continue
        pid = _proposal_id(prop.skill_name, prop.new_fitness)
        row = {
            "id": pid,
            "proposal_id": pid,
            "skill_name": prop.skill_name,
            "current_trigger": prop.current_trigger,
            "new_trigger": prop.new_trigger,
            "current_body": prop.current_body,
            "new_body": prop.new_body,
            "current_fitness": prop.current_fitness,
            "new_fitness": prop.new_fitness,
            "improves": prop.improves,
            "rationale": prop.rationale,
            "diff": prop.diff(),
            "source": prop.source,
            "status": "pending",
        }
        if st is not None:
            try:
                st.upsert_evolution_proposal(
                    pid, prop.skill_name,
                    current_trigger=prop.current_trigger,
                    new_trigger=prop.new_trigger,
                    current_body=prop.current_body,
                    new_body=prop.new_body,
                    current_fitness=prop.current_fitness,
                    new_fitness=prop.new_fitness,
                    improves=prop.improves,
                    rationale=prop.rationale or "",
                    diff_text=prop.diff(),
                    source=prop.source or "",
                    payload={"skill_name": prop.skill_name},
                    status="pending",
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning("persist proposal failed: %s", exc)
                _mem_upsert(row)
        else:
            _mem_upsert(row)
        public.append(_row_public(row))
    # Return full pending queue (durable or mem)
    return list_proposals(store=st)


def _mem_upsert(row: dict) -> None:
    global _PROPOSALS
    skill = row.get("skill_name")
    _PROPOSALS = [p for p in _PROPOSALS if p.get("skill_name") != skill]
    _PROPOSALS.append(row)


def list_proposals(store: "Store | None" = None) -> list[dict]:
    st = store if store is not None else _PROP_STORE
    if st is not None:
        try:
            return [_row_public(r) for r in st.list_evolution_proposals(
                status="pending")]
        except Exception as exc:  # noqa: BLE001
            _log.warning("list proposals failed: %s", exc)
    return [_row_public(p) for p in _PROPOSALS if p.get("status", "pending") == "pending"]


def apply_proposal(agent, proposal_id: str,
                   store: "Store | None" = None) -> dict:
    global _PROPOSALS
    from . import evolution as evo
    from .evolution import Proposal
    st = store if store is not None else _PROP_STORE
    row = None
    if st is not None:
        try:
            row = st.get_evolution_proposal(proposal_id)
        except Exception as exc:  # noqa: BLE001
            _log.warning("load proposal failed: %s", exc)
    if row is None:
        row = next((p for p in _PROPOSALS if p.get("id") == proposal_id
                    or p.get("proposal_id") == proposal_id), None)
    if not row:
        return {"error": "proposal not found"}
    if row.get("status") and row.get("status") not in ("pending",):
        return {"error": f"proposal status is {row.get('status')}"}

    prop = Proposal(
        skill_name=row["skill_name"],
        current_trigger=row.get("current_trigger") or "",
        current_body=row.get("current_body") or "",
        new_trigger=row.get("new_trigger") or "",
        new_body=row.get("new_body") or "",
        current_fitness=float(row.get("current_fitness") or 0),
        new_fitness=float(row.get("new_fitness") or 0),
        source=row.get("source") or "stored",
        rationale=row.get("rationale") or "",
    )
    lib = getattr(agent, "skills", None)
    if lib is None:
        from .persistence import Store
        from .skills import SkillLibrary
        lib = SkillLibrary(store=st or Store.open())
    ok = evo.apply_proposal(lib, prop)
    if ok:
        if st is not None:
            try:
                st.resolve_evolution_proposal(proposal_id, "applied")
            except Exception as exc:  # noqa: BLE001
                _log.warning("resolve proposal failed: %s", exc)
        _PROPOSALS = [p for p in _PROPOSALS
                      if p.get("id") != proposal_id
                      and p.get("proposal_id") != proposal_id]
    return {"applied": bool(ok), "skill": prop.skill_name}


def reject_proposal(proposal_id: str,
                    store: "Store | None" = None) -> dict:
    global _PROPOSALS
    st = store if store is not None else _PROP_STORE
    rejected = False
    if st is not None:
        try:
            rejected = st.resolve_evolution_proposal(proposal_id, "rejected")
        except Exception as exc:  # noqa: BLE001
            _log.warning("reject proposal failed: %s", exc)
    before = len(_PROPOSALS)
    _PROPOSALS = [p for p in _PROPOSALS
                  if p.get("id") != proposal_id
                  and p.get("proposal_id") != proposal_id]
    if len(_PROPOSALS) < before:
        rejected = True
    return {"rejected": rejected, "id": proposal_id}


def user_model_card() -> dict:
    from .persona import load_persona
    p = load_persona()
    return {
        "persona": p,
        "summary": _summary(p),
    }


def _summary(p: dict) -> str:
    bits = []
    if p.get("display_name"):
        bits.append(p["display_name"])
    if p.get("role"):
        bits.append(p["role"])
    if p.get("tone"):
        bits.append(f"tone: {p['tone']}")
    if p.get("never_do"):
        bits.append("never: " + ", ".join(p["never_do"][:4]))
    return " · ".join(bits) if bits else "No persona yet — complete onboarding."


def record_ttft(seconds: float) -> dict:
    conf = cfg.load_config()
    metrics = conf.setdefault("agents", {}).setdefault("metrics", {})
    samples = list(metrics.get("ttft_seconds") or [])
    samples.append(round(float(seconds), 3))
    samples = samples[-50:]
    metrics["ttft_seconds"] = samples
    metrics["ttft_p50"] = sorted(samples)[len(samples) // 2] if samples else None
    cfg.save_config(conf)
    return {"samples": len(samples), "p50": metrics["ttft_p50"], "last": samples[-1]}


def ttft_stats() -> dict:
    conf = cfg.load_config()
    metrics = ((conf.get("agents") or {}).get("metrics") or {})
    samples = list(metrics.get("ttft_seconds") or [])
    return {
        "samples": samples,
        "count": len(samples),
        "p50": metrics.get("ttft_p50"),
        "last": samples[-1] if samples else None,
    }


def list_rooms() -> list[dict]:
    conf = cfg.load_config()
    rooms = ((conf.get("agents") or {}).get("rooms") or [])
    if rooms:
        return list(rooms)
    return [
        {"id": "main", "name": "Main", "role": "general",
         "desc": "Strategy, planning, default colleague"},
        {"id": "research", "name": "Researcher", "role": "research",
         "desc": "Web + knowledge research"},
        {"id": "ops", "name": "Ops", "role": "ops",
         "desc": "Cron, digests, status notes"},
        {"id": "writer", "name": "Writer", "role": "draft",
         "desc": "Drafts held for approval"},
    ]


def save_rooms(rooms: list[dict]) -> list[dict]:
    conf = cfg.load_config()
    conf.setdefault("agents", {})["rooms"] = rooms
    cfg.save_config(conf)
    return list_rooms()
