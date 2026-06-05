"""tick.cli — argparse wiring over tick.core + tick.store."""

from __future__ import annotations

import argparse
import sys

from tick import core
from tick import store


def _strip(id: str) -> str:
    return id.removeprefix("!")


def cmd_init(args) -> int:
    st = store.init(remote=args.remote, store_path=args.store, install_guard=args.install_guard)
    print(f"tick initialized")
    print(f"  store:  {st.worktree}")
    print(f"  branch: {st.branch}")
    print(f"  remote: {st.remote or '(none — set later with tick.remote)'}")
    return 0


def cmd_add(args) -> int:
    st = store.resolve()
    id = store.add(st, args.title, kind=args.kind, tags=args.tags)
    print(f"!{id}  {args.title}")
    print(f"paste the mark  !{id}  wherever this work lives in the code")
    return 0


def cmd_note(args) -> int:
    st = store.resolve()
    store.note(st, _strip(args.id), args.text)
    print(f"noted on !{_strip(args.id)}")
    return 0


def cmd_edit(args) -> int:
    st = store.resolve()
    changed = store.edit(st, _strip(args.id))
    print("saved" if changed else "no change")
    return 0


def cmd_off(args) -> int:
    st = store.resolve()
    id = _strip(args.id)
    sites = store.off(st, id)
    print(f"ticked off !{id}")
    if sites:
        print(f"warning: !{id} is still referenced in the code — remove the mark:")
        for path, lineno, line in sites:
            print(f"  {path}:{lineno}: {line}")
    return 0


def cmd_reopen(args) -> int:
    st = store.resolve()
    store.reopen(st, _strip(args.id))
    print(f"reopened !{_strip(args.id)}")
    return 0


def cmd_ls(args) -> int:
    st = store.resolve()
    ticks = core.filter_ticks(
        store.list_ticks(st),
        include_closed=args.all,
        only_closed=args.closed,
        kind=args.kind,
        tag=args.tag,
    )
    if not ticks:
        print("(no ticks)")
        return 0
    for t in ticks:
        suffix = "  (closed)" if not t.is_open else ""
        print(f"!{t.id}  {t.kind:5}  {t.title}{suffix}")
    return 0


def cmd_show(args) -> int:
    st = store.resolve()
    print(core.serialize_tick(store.read_tick(st, _strip(args.id))), end="")
    return 0


def cmd_grep(args) -> int:
    st = store.resolve()
    hits = store.grep(st, args.query)
    if not hits:
        print("(no matches)")
        return 0
    for t in hits:
        print(f"!{t.id}  {t.title}")
    return 0


def cmd_refs(args) -> int:
    st = store.resolve()
    sites = store.refs(st, _strip(args.id))
    if not sites:
        print("(no references in code)")
        return 0
    for path, lineno, line in sites:
        print(f"{path}:{lineno}: {line}")
    return 0


def cmd_orphans(args) -> int:
    st = store.resolve()
    marks_without_tick, open_without_mark = store.orphans(st)
    print("marks in code with no tick file:")
    for m in sorted(marks_without_tick):
        print(f"  !{m}")
    if not marks_without_tick:
        print("  (none)")
    print("open ticks with no mark in code:")
    for m in sorted(open_without_mark):
        print(f"  !{m}")
    if not open_without_mark:
        print("  (none)")
    return 0


def cmd_sync(args) -> int:
    st = store.resolve()
    store.sync(st)
    print("synced")
    return 0


def cmd_link(args) -> int:
    st = store.resolve()
    created = store.link(st)
    print("linked .tick -> store" if created else ".tick already present")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tick", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", help="set up the tick ledger in this repo")
    sp.add_argument("--remote", help="remote to push the tick branch to (default: origin)")
    sp.add_argument("--store", help="store path (default: <repo-root>/.tick)")
    sp.add_argument("--install-guard", action="store_true", help="install the pre-push guard")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("add", help="add a tick; prints the mark to paste into code")
    sp.add_argument("title")
    sp.add_argument("--kind", choices=core.VALID_KINDS, default=core.DEFAULT_KIND)
    sp.add_argument("--tag", action="append", default=[], dest="tags")
    sp.set_defaults(func=cmd_add)

    sp = sub.add_parser("note", help="append a dated note to a tick")
    sp.add_argument("id")
    sp.add_argument("text")
    sp.set_defaults(func=cmd_note)

    sp = sub.add_parser("edit", help="open a tick in $EDITOR to correct/rewrite")
    sp.add_argument("id")
    sp.set_defaults(func=cmd_edit)

    sp = sub.add_parser("off", help="tick off (close) a tick")
    sp.add_argument("id")
    sp.set_defaults(func=cmd_off)

    sp = sub.add_parser("reopen", help="reopen a closed tick")
    sp.add_argument("id")
    sp.set_defaults(func=cmd_reopen)

    sp = sub.add_parser("ls", help="list ticks (open by default)")
    sp.add_argument("--all", action="store_true", help="include closed")
    sp.add_argument("--closed", action="store_true", help="only closed")
    sp.add_argument("--kind", choices=core.VALID_KINDS)
    sp.add_argument("--tag")
    sp.set_defaults(func=cmd_ls)

    sp = sub.add_parser("show", help="print a tick")
    sp.add_argument("id")
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("grep", help="search tick titles/bodies")
    sp.add_argument("query")
    sp.set_defaults(func=cmd_grep)

    sp = sub.add_parser("refs", help="find a tick's mark sites in the code")
    sp.add_argument("id")
    sp.set_defaults(func=cmd_refs)

    sub.add_parser("orphans", help="lint marks vs ticks").set_defaults(func=cmd_orphans)
    sub.add_parser("sync", help="pull --rebase then push the tick branch").set_defaults(func=cmd_sync)
    sub.add_parser("link", help="add a .tick symlink in this worktree").set_defaults(func=cmd_link)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except store.TickError as e:
        print(f"tick: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
