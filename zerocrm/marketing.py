"""Marketing = the existing outbound spine, turned on for the customer base. No new
tables, no new sends: an audience is built from customers (won deals + active
contracts) and enrolled into a nurture campaign as `scheduled`; the digest approval
and sending_enabled gate still own every touch."""

from __future__ import annotations

from sqlalchemy import insert, select
from sqlalchemy.engine import Engine

from .schema import campaign, contract, deal, flow_state
from .schema import list_ as list_t
from .schema import list_member, person


def customer_audience(engine: Engine, workspace_id: str = "default") -> list[str]:
    """Deduped person ids for existing customers: a WON deal, or a contact at a
    company with an ACTIVE contract. Cold prospects (neither) are excluded."""
    with engine.connect() as conn:
        won = select(deal.c.person_id).where(deal.c.stage == "won",
                                             deal.c.person_id.isnot(None))
        active_companies = select(contract.c.company_id).where(
            contract.c.status == "active", contract.c.company_id.isnot(None))
        by_contract = select(person.c.id).where(person.c.company_id.in_(active_companies))
        ids = {r[0] for r in conn.execute(won)} | {r[0] for r in conn.execute(by_contract)}
    return sorted(i for i in ids if i)


def build_customer_list(engine: Engine, *, name, actor, workspace_id: str = "default") -> str:
    people = customer_audience(engine, workspace_id)
    with engine.begin() as conn:
        lid = conn.execute(insert(list_t).values(
            name=name, purpose="audience", actor=actor,
            workspace_id=workspace_id)).inserted_primary_key[0]
        for pid in people:
            conn.execute(insert(list_member).values(
                list_id=lid, person_id=pid, actor=actor, workspace_id=workspace_id))
    return lid


def create_nurture_campaign(engine: Engine, *, name, channel: str = "email", actor,
                            workspace_id: str = "default") -> str:
    with engine.begin() as conn:
        return conn.execute(insert(campaign).values(
            name=name, channel=channel, flow_kind="nurture", status="active",
            actor=actor, workspace_id=workspace_id)).inserted_primary_key[0]


def enrol_list(engine: Engine, list_id: str, campaign_id: str, *, actor,
               workspace_id: str = "default") -> int:
    """One scheduled flow_state per list member, idempotent on (person, campaign).
    Scheduled (not active) respects the one-active-flow-per-(person,channel) index;
    the digest + sending_enabled gate still own whether anything sends."""
    with engine.begin() as conn:
        channel = conn.execute(select(campaign.c.channel)
                               .where(campaign.c.id == campaign_id)).scalar() or "email"
        members = [r[0] for r in conn.execute(
            select(list_member.c.person_id).where(list_member.c.list_id == list_id))]
        enrolled = 0
        for pid in members:
            exists = conn.execute(select(flow_state.c.id).where(
                flow_state.c.person_id == pid,
                flow_state.c.campaign_id == campaign_id)).first()
            if exists:
                continue
            conn.execute(insert(flow_state).values(
                person_id=pid, campaign_id=campaign_id, channel=channel, step=0,
                status="scheduled", actor=actor, workspace_id=workspace_id))
            enrolled += 1
    return enrolled
