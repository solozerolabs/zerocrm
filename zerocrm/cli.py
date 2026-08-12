"""zerocrm CLI skeleton — stdlib argparse, no click dependency.

Phase 1 exposes the two verbs that need no drivers: `migrate` and `add-person`
(the latter proves the precedence gate end to end from a shell).
"""

from __future__ import annotations

import argparse
import json
import sys

from .config import get_config, set_config
from .db import make_engine
from .migrate import migrate
from .runner import load_runtime_config, run_digest, run_slack_tick, run_tick
from .upsert import upsert_person


def _cmd_migrate(args: argparse.Namespace) -> int:
    engine = make_engine(args.dsn)
    ran = migrate(engine)
    print(f"applied: {ran}" if ran else "already up to date")
    return 0


def _cmd_add_person(args: argparse.Namespace) -> int:
    engine = make_engine(args.dsn)
    fields = json.loads(args.fields) if args.fields else {}
    with engine.begin() as conn:
        pid = upsert_person(
            conn,
            identity={"kind": "email", "value": args.email},
            fields=fields,
            provenance=args.provenance,
            actor={"kind": "human", "id": "cli"},
        )
    print(pid)
    return 0


def _cmd_digest(args: argparse.Namespace) -> int:
    print(json.dumps(run_digest(make_engine(args.dsn)), default=str))
    return 0


def _cmd_tick(args: argparse.Namespace) -> int:
    print(json.dumps(run_tick(make_engine(args.dsn)), default=str))
    return 0


def _cmd_slack_tick(args: argparse.Namespace) -> int:
    print(json.dumps(run_slack_tick(make_engine(args.dsn)), default=str))
    return 0


def _cmd_ingest_doc(args: argparse.Namespace) -> int:
    from .memory import ingest_document
    with open(args.path) as f:
        text = f.read()
    n = ingest_document(make_engine(args.dsn), text=text, source_id=args.path,
                        deal_id=args.deal, person_id=args.person)
    print(f"{n} chunks")
    return 0


def _cmd_set_config(args: argparse.Namespace) -> int:
    # value parsed as JSON so flags/lists work: sending_enabled true, campaigns '[77]'
    set_config(make_engine(args.dsn), args.key, json.loads(args.value))
    print(f"{args.key} = {args.value}")
    return 0


def _cmd_get_config(args: argparse.Namespace) -> int:
    print(json.dumps(get_config(make_engine(args.dsn), args.key), default=str))
    return 0


def _cmd_recopy(args: argparse.Namespace) -> int:
    """Rebuild copy on all pending drafts in place (voice step if wired, else
    the deterministic floor). No provider spend, no new enrollments."""
    from .draft_run import lint_pending_drafts, recopy_drafts
    from .runner import _build_copy_fn
    engine = make_engine(args.dsn)
    cfg = load_runtime_config(engine)
    draft = cfg.get("draft")
    if not draft:
        print("no draft config set")
        return 1
    ws = cfg.get("workspace_id", "default")
    updated = recopy_drafts(engine, draft, icebreaker_fn=_build_copy_fn(engine), workspace_id=ws)
    print(f"recopied {len(updated)} drafts: {updated}")
    # backstop: recopy enforces lint at write, so this should be empty. If not,
    # the fallback floor itself is producing a violation — surface it, don't hide.
    residual = lint_pending_drafts(engine, ws)
    for item_no, violations in residual:
        print(f"  STILL DIRTY draft {item_no}: {'; '.join(violations)}")
    return 1 if residual else 0


def _cmd_lint_drafts(args: argparse.Namespace) -> int:
    """Gate: fail (exit 1) if any pending draft has a copy violation."""
    from .draft_run import lint_pending_drafts
    engine = make_engine(args.dsn)
    cfg = load_runtime_config(engine)
    bad = lint_pending_drafts(engine, cfg.get("workspace_id", "default"))
    for item_no, violations in bad:
        print(f"draft {item_no}: {'; '.join(violations)}")
    print(f"{len(bad)} draft(s) with violations" if bad else "all drafts clean")
    return 1 if bad else 0


def _cmd_verify_live(args: argparse.Namespace) -> int:
    """Pre-flip check: confirm the live Smartlead shapes before enabling sending."""
    from .drivers.smartlead import Smartlead
    engine = make_engine(args.dsn)
    cfg = load_runtime_config(engine)
    sl = Smartlead()
    accts = sl.list_email_accounts()
    print(f"smartlead accounts: {[a.get('from_email') for a in accts]}")
    for cid in cfg.get("smartlead_poll_campaigns", []):
        try:
            evs = sl.poll_events(cid, "2026-01-01", "2026-12-31")
            print(f"poll campaign {cid}: {len(evs)} events, shape OK")
        except Exception as exc:  # noqa: BLE001
            print(f"poll campaign {cid}: FAILED -> {exc}")
    print("If all green, flip: zerocrm set-config sending_enabled true")
    return 0


_CLI_ACTOR = {"kind": "human", "id": "cli"}


def _cmd_add_location(args: argparse.Namespace) -> int:
    from .schema import location
    with make_engine(args.dsn).begin() as conn:
        lid = conn.execute(location.insert().values(
            name=args.name, company_id=args.company, actor=_CLI_ACTOR)).inserted_primary_key[0]
    print(lid)
    return 0


def _cmd_add_staff(args: argparse.Namespace) -> int:
    from .schema import staff
    with make_engine(args.dsn).begin() as conn:
        sid = conn.execute(staff.insert().values(
            full_name=args.name, email=args.email, role=args.role,
            actor=_CLI_ACTOR)).inserted_primary_key[0]
    print(sid)
    return 0


def _cmd_new_contract(args: argparse.Namespace) -> int:
    from .contracts import create_contract
    cid = create_contract(make_engine(args.dsn), company_id=args.company,
                          location_id=args.location, contract_type=args.type,
                          scope=args.scope, actor=_CLI_ACTOR)
    print(cid)
    return 0


def _cmd_contract_history(args: argparse.Namespace) -> int:
    from .contracts import contract_history
    print(json.dumps(contract_history(make_engine(args.dsn), args.contract), default=str))
    return 0


def _cmd_attach_doc(args: argparse.Namespace) -> int:
    from .documents import attach_document
    text = None
    if args.file:
        with open(args.file) as f:
            text = f.read()
    data = None
    if args.upload:
        with open(args.upload, "rb") as f:
            data = f.read()
    did = attach_document(make_engine(args.dsn), contract_id=args.contract,
                          location_id=args.location, filename=args.filename,
                          uri=args.uri, text=text, data=data, actor=_CLI_ACTOR)
    print(did)
    return 0


def _cmd_verify_storage(args: argparse.Namespace) -> int:
    from .storage import from_env
    store = from_env()
    if store is None:
        print("blob storage NOT configured (set ZEROCRM_S3_ENDPOINT/BUCKET/ACCESS_KEY_ID/SECRET_ACCESS_KEY)")
        return 1
    key = "zerocrm-selftest/verify.txt"
    store.put(key, b"ok", content_type="text/plain")
    ok = store.get(key) == b"ok"
    store.delete(key)
    print("storage OK: put/get/delete round-trip verified" if ok else "storage FAILED round-trip")
    return 0 if ok else 1


def _cmd_record_inspection(args: argparse.Namespace) -> int:
    from .inspections import record_inspection
    iid = record_inspection(make_engine(args.dsn), location_id=args.location,
                            contract_id=args.contract, outcome=args.outcome,
                            score=args.score, notes=args.notes or "")
    print(iid)
    return 0


def _cmd_add_vendor(args: argparse.Namespace) -> int:
    from .pricing import add_vendor
    print(add_vendor(make_engine(args.dsn), name=args.name, actor=_CLI_ACTOR))
    return 0


def _cmd_add_supply(args: argparse.Namespace) -> int:
    from .pricing import add_supply
    print(add_supply(make_engine(args.dsn), name=args.name, vendor_id=args.vendor,
                     current_cost_usd=args.cost, actor=_CLI_ACTOR))
    return 0


def _cmd_price_change(args: argparse.Namespace) -> int:
    from .pricing import flag_price_impacts, record_price_change
    engine = make_engine(args.dsn)
    record_price_change(engine, args.supply, args.cost, actor=_CLI_ACTOR)
    n = flag_price_impacts(engine, args.supply)
    print(f"price recorded; {n} active contract(s) flagged")
    return 0


def _cmd_new_quote(args: argparse.Namespace) -> int:
    from .quotes import create_quote
    print(create_quote(make_engine(args.dsn), company_id=args.company,
                       contract_id=args.contract, deal_id=args.deal,
                       margin_pct=args.margin, actor=_CLI_ACTOR))
    return 0


def _cmd_quote_line(args: argparse.Namespace) -> int:
    from .quotes import add_quote_line
    print(add_quote_line(make_engine(args.dsn), args.quote, supply_id=args.supply,
                         description=args.description, qty=args.qty,
                         unit_cost_usd=args.cost, actor=_CLI_ACTOR))
    return 0


def _cmd_nurture(args: argparse.Namespace) -> int:
    from .marketing import build_customer_list, create_nurture_campaign, enrol_list
    engine = make_engine(args.dsn)
    lid = build_customer_list(engine, name=f"{args.name}-audience", actor=_CLI_ACTOR)
    cid = create_nurture_campaign(engine, name=args.name, channel=args.channel, actor=_CLI_ACTOR)
    n = enrol_list(engine, lid, cid, actor=_CLI_ACTOR)
    print(f"nurture '{args.name}': {n} customer(s) enrolled (scheduled)")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="zerocrm")
    p.add_argument("--dsn", default=None, help="DB DSN (default: env or sqlite:///zerocrm.db)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("migrate", help="apply pending migrations").set_defaults(func=_cmd_migrate)
    sub.add_parser("digest", help="draft (if configured) + email the operator digest").set_defaults(func=_cmd_digest)
    sub.add_parser("tick", help="poll replies/bounces + process operator replies").set_defaults(func=_cmd_tick)
    sub.add_parser("slack-tick", help="poll the Slack DM surface + answer pre-call briefs").set_defaults(func=_cmd_slack_tick)

    ing = sub.add_parser("ingest-doc", help="chunk a file into the retrieval spine")
    ing.add_argument("path")
    ing.add_argument("--deal", default=None)
    ing.add_argument("--person", default=None)
    ing.set_defaults(func=_cmd_ingest_doc)
    sub.add_parser("recopy", help="rebuild copy on all pending drafts in place").set_defaults(func=_cmd_recopy)
    sub.add_parser("lint-drafts", help="fail if any pending draft has a copy violation").set_defaults(func=_cmd_lint_drafts)
    sub.add_parser("verify-live", help="pre-flip: confirm live Smartlead shapes").set_defaults(func=_cmd_verify_live)

    ap = sub.add_parser("add-person", help="upsert a person through the precedence gate")
    ap.add_argument("email")
    ap.add_argument("--fields", help='JSON, e.g. {"full_name":"Jane","job_title":"CEO"}')
    ap.add_argument("--provenance", default="imported")
    ap.set_defaults(func=_cmd_add_person)

    al = sub.add_parser("add-location", help="add a customer location")
    al.add_argument("--name", required=True)
    al.add_argument("--company", default=None)
    al.set_defaults(func=_cmd_add_location)

    ast = sub.add_parser("add-staff", help="add a staff member (customer employee)")
    ast.add_argument("--name", required=True)
    ast.add_argument("--email", default=None)
    ast.add_argument("--role", default=None)
    ast.set_defaults(func=_cmd_add_staff)

    ncn = sub.add_parser("new-contract", help="create a service contract")
    ncn.add_argument("--company", default=None)
    ncn.add_argument("--location", default=None)
    ncn.add_argument("--type", default=None)
    ncn.add_argument("--scope", default=None)
    ncn.set_defaults(func=_cmd_new_contract)

    chy = sub.add_parser("contract-history", help="print a contract's audit trail")
    chy.add_argument("contract")
    chy.set_defaults(func=_cmd_contract_history)

    adc = sub.add_parser("attach-doc", help="attach a doc to a contract/location")
    adc.add_argument("--filename", required=True)
    adc.add_argument("--contract", default=None)
    adc.add_argument("--location", default=None)
    adc.add_argument("--uri", default=None)
    adc.add_argument("--file", default=None, help="local text file to also chunk for retrieval")
    adc.add_argument("--upload", default=None, help="local file to upload to blob storage (sets uri)")
    adc.set_defaults(func=_cmd_attach_doc)

    sub.add_parser("verify-storage", help="probe blob storage (put/get/delete round-trip)").set_defaults(func=_cmd_verify_storage)

    ri = sub.add_parser("record-inspection", help="record a quality inspection")
    ri.add_argument("--location", required=True)
    ri.add_argument("--contract", default=None)
    ri.add_argument("--outcome", required=True)
    ri.add_argument("--score", type=int, default=None)
    ri.add_argument("--notes", default=None)
    ri.set_defaults(func=_cmd_record_inspection)

    av = sub.add_parser("add-vendor", help="add a supply vendor")
    av.add_argument("--name", required=True)
    av.set_defaults(func=_cmd_add_vendor)

    asp = sub.add_parser("add-supply", help="add a supply to the master list")
    asp.add_argument("--name", required=True)
    asp.add_argument("--vendor", default=None)
    asp.add_argument("--cost", type=float, default=None)
    asp.set_defaults(func=_cmd_add_supply)

    pc = sub.add_parser("price-change", help="record a vendor price change + flag impacts")
    pc.add_argument("--supply", required=True)
    pc.add_argument("--cost", type=float, required=True)
    pc.set_defaults(func=_cmd_price_change)

    nq = sub.add_parser("new-quote", help="create a customer quote")
    nq.add_argument("--company", default=None)
    nq.add_argument("--contract", default=None)
    nq.add_argument("--deal", default=None)
    nq.add_argument("--margin", type=float, default=0.0)
    nq.set_defaults(func=_cmd_new_quote)

    ql = sub.add_parser("quote-line", help="add a line to a quote")
    ql.add_argument("--quote", required=True)
    ql.add_argument("--supply", default=None)
    ql.add_argument("--description", default=None)
    ql.add_argument("--qty", type=float, default=1)
    ql.add_argument("--cost", type=float, default=None)
    ql.set_defaults(func=_cmd_quote_line)

    nu = sub.add_parser("nurture", help="enrol the customer base into a nurture campaign")
    nu.add_argument("--name", required=True)
    nu.add_argument("--channel", default="email")
    nu.set_defaults(func=_cmd_nurture)

    sc = sub.add_parser("set-config", help="set a config key (value is JSON)")
    sc.add_argument("key")
    sc.add_argument("value", help='JSON value, e.g. true or [77] or "operator@example.com"')
    sc.set_defaults(func=_cmd_set_config)

    gc = sub.add_parser("get-config", help="read a config key")
    gc.add_argument("key")
    gc.set_defaults(func=_cmd_get_config)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
