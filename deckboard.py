#!/usr/bin/env python3
"""deckboard.py — what has been solved, what has been drawn, and which is stale.

    python3 scripts/deckboard.py            # the window
    python3 scripts/deckboard.py --report   # the same data as text, no window

WHY THIS EXISTS. The store and the deck folders answer different halves of one question and
neither can answer it alone. `simulations/<model>/<experiment>/<variant>/<stamp>/run.h5` says
what has been SOLVED; `output/<deck>/*.png` says what has been DRAWN. Nothing on disk joins
them — the deck's provenance stamp is only written when `_STAMP_OUT` is set, and it is
deliberately kept out of `output/` so it cannot be committed. So "is this figure still
telling the truth about the runs it came from?" was a question you answered by reading
timestamps by hand, across two trees, against a deck→runs mapping that lives in a `?:` chain
in the middle of plotting.jl.

That last part is why this reads the repo instead of restating it. The registry, the
deck→models chain and the experiment suffixes are all PARSED from the Julia sources at
startup. A hand-copied registry in a Python file would be wrong the first time a run id is
added, and wrong silently — which is the exact failure the deck folders already had.

STALENESS is the point of the whole thing. For each deck it resolves the run ids that deck
overlays, finds the export each id resolves to TODAY (newest vintage, same rule plot_runs.jl
uses), and compares that export's mtime against the PNG's. An export newer than the picture
means the picture is of an older solve, and nothing in the picture says so.

NO NEW DEPENDENCIES — tkinter, subprocess and the stdlib, plus simstore.py from the repo
root. It is a .py rather than a notebook because it has to own a window and a subprocess for
the length of a solve, which a notebook cell does not do well; `--report` is there for when
you want the numbers in a terminal or a notebook instead.

RUNNING A SOLVE IS TWO CLICKS, NEVER ONE. Building the command and executing it are separate
buttons, because a deck run ends by uploading its PNGs to Google Drive — a misclick is an
outward-facing action, not just some wasted minutes.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import queue
from dataclasses import dataclass, field
from datetime import datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import simstore  # noqa: E402  — the store reader, already written and already correct


# ══════════════════════════════════════════════════════════════════════════════
# READING THE REPO
# ══════════════════════════════════════════════════════════════════════════════
# Three facts live in Julia and are parsed out of it rather than duplicated here. Each
# parser raises if it finds nothing: a regex that silently stops matching would leave this
# window showing an empty, confident, wrong picture — worse than failing to open.

REGISTRY_JL = os.path.join(REPO, "Model_3", "registry.jl")
PLOTTING_JL = os.path.join(REPO, "Model_3", "plotting.jl")


def _uncommented(path: str) -> str:
    """`#` to end of line, unless a quote comes first — the same crude strip the Julia
    test suite uses. Load-bearing: registry.jl and plotting.jl both carry commented-out
    rows and commented-out deck branches, and a commented row is not a row."""
    out = []
    for line in open(path, encoding="utf-8").read().split("\n"):
        q, h = line.find('"'), line.find("#")
        out.append(line if h < 0 or (0 <= q < h) else line[:h])
    return "\n".join(out)


# The variant folder, mirroring registry.jl's `_shunt_tag` / `_variant`. Reimplemented
# rather than parsed because it is four lines of Julia with no data in it — but the SHUNT
# SYMBOLS it switches on are parsed, so a new shunt kind shows up as a KeyError here rather
# than as a silently wrong path.
_SHUNT_TAG = {"none": "", "va": "_vashunt", "vp": "_vpshunt",
              "va_asd": "_vashunt_asd", "vp_asd": "_vpshunt_asd"}


@dataclass(frozen=True)
class Row:
    id: str
    model: str
    variant: str
    label: str
    color: str


def parse_registry() -> dict[str, Row]:
    """MODEL_REGISTRY, in both of the forms registry.jl writes.

    `_row(model, puls, shunt, label, color)` for the ordinary rows, and the explicit
    named-tuple form the bistability basins use (their variant is not derived from
    pulsatile/shunt at all — the basin IS the variant).
    """
    src = _uncommented(REGISTRY_JL)
    rows: dict[str, Row] = {}

    for m in re.finditer(
        r':(\w+)\s*=>\s*_row\(\s*"(\w+)"\s*,\s*(true|false)\s*,\s*:(\w+)\s*,\s*"([^"]*)"\s*,\s*"([^"]*)"',
        src,
    ):
        rid, model, puls, shunt, label, color = m.groups()
        variant = ("pulsatile" if puls == "true" else "nonpulsatile") + _SHUNT_TAG[shunt]
        rows[rid] = Row(rid, model, variant, label, color)

    for m in re.finditer(
        r':(\w+)\s*=>\s*\(\s*model\s*=\s*"(\w+)"\s*,\s*variant\s*=\s*"(\w+)"\s*,\s*label\s*=\s*"([^"]*)"\s*,\s*color\s*=\s*"([^"]*)"',
        src,
    ):
        rid, model, variant, label, color = m.groups()
        rows[rid] = Row(rid, model, variant, label, color)

    if len(rows) < 20:
        raise SystemExit(f"deckboard: parsed only {len(rows)} registry rows from "
                         f"{REGISTRY_JL} — the row syntax moved, fix parse_registry().")
    return rows


def parse_deck_models() -> tuple[list[tuple[str, list[str]]], list[str]]:
    """The `endswith(DECK, "_suffix") ? [ids] : …` chain in plotting.jl, in ORDER, plus the
    fallback at its end. Order matters and is preserved: `_bistability_m3_analysis` has to
    be tested before `_m3_analysis`, exactly as the Julia does."""
    src = _uncommented(PLOTTING_JL)
    block = re.search(r"const MODELS = let over(.*?)\n\s*let ids", src, re.S)
    if not block:
        raise SystemExit("deckboard: could not find the MODELS chain in plotting.jl.")
    body = block.group(1)
    chain = [(m.group(1), re.findall(r":(\w+)", m.group(2)))
             for m in re.finditer(r'endswith\(DECK,\s*"([^"]+)"\)\s*\?\s*\[([^\]]*)\]', body)]
    tail = re.findall(r":\s*\n\s*\[([^\]]*)\]\)?\s*:", body)
    fallback = re.findall(r":(\w+)", tail[-1]) if tail else ["m3p", "m3"]
    if len(chain) < 8:
        raise SystemExit(f"deckboard: parsed only {len(chain)} deck→models branches — "
                         "the chain in plotting.jl moved.")
    return chain, fallback


def parse_experiment_suffixes() -> list[str]:
    """The analysis suffixes registry.jl strips to get EXPERIMENT. Parsed from the same
    alternation, so a suffix added there is honoured here without an edit."""
    src = _uncommented(REGISTRY_JL)
    m = re.search(r'r"_\(([^)]*)\)\$"\s*=>\s*""', src)
    if not m:
        raise SystemExit("deckboard: could not find the EXPERIMENT suffix regex.")
    return [s for s in m.group(1).split("|") if s]


REGISTRY = parse_registry()
DECK_CHAIN, DECK_FALLBACK = parse_deck_models()
SUFFIXES = parse_experiment_suffixes()


def experiment_of(deck: str) -> str:
    for suf in SUFFIXES:
        if deck.endswith("_" + suf):
            return deck[: -len(suf) - 1]
    return deck


def models_of(deck: str) -> list[str]:
    for suffix, ids in DECK_CHAIN:
        if deck.endswith(suffix):
            return ids
    return DECK_FALLBACK


# --- What KIND of folder is this? --------------------------------------------
# Three kinds, and conflating them is how a status board starts lying:
#
#   "analysis"  <experiment>_<suffix>, drawn by plotting.jl over MODEL_REGISTRY ids.
#   "sweep"     <experiment>_<label>_sweep, drawn by plotting_sweep.jl. Its runs are ONE
#               store family per model — main_sweep.jl:442/453, SWEEP_DIR against
#               SWEEP_VARIANT = "sweep_<label>" — and no registry id names them, so
#               resolving it through MODEL_REGISTRY finds nothing and reports a folder full
#               of good figures as having no exports at all.
#   "unmapped"  everything else under output/ — latex/, ursino_validation/, loose PNGs,
#               folders from a deck name this checkout no longer knows. `models_of` has a
#               FALLBACK ([:m3p, :m3]) because _DECK accepts anything, and applying it here
#               would invent two runs for a folder that never had them.
#
# Only the first two get a staleness verdict. An unmapped folder is listed and left alone:
# "I do not know what this is" is a true statement, and "NO EXPORT" is not.

def deck_kind(deck: str) -> str:
    if any(deck.endswith("_" + s) for s in SUFFIXES):
        return "analysis"
    if deck.endswith("_sweep"):
        return "sweep"
    return "unmapped"


def sweep_family(deck: str) -> tuple[str, str]:
    """`postural_changes_h_Pext_sweep` -> ("postural_changes", "sweep_h_Pext").

    The MODEL is not in the deck name — a sweep is one model with one parameter moved, and
    which model ran it is only recorded in the store. So the caller matches on
    (experiment, variant) across every model rather than assuming m3.
    """
    stem = deck[: -len("_sweep")]
    experiment = stem
    label = ""
    # Longest experiment prefix that some family in the store actually has, so this does
    # not have to hardcode the experiment list — `postural_changes_CGk_sweep_k_sweep` is a
    # sweep OF a sweep folder and only the store can say where its cut is.
    for exp in sorted({v.experiment for v in STORE.inventory()}, key=len, reverse=True):
        if stem.startswith(exp + "_"):
            experiment, label = exp, stem[len(exp) + 1:]
            break
    return experiment, ("sweep_" + label if label else "")


# ══════════════════════════════════════════════════════════════════════════════
# THE TWO INVENTORIES
# ══════════════════════════════════════════════════════════════════════════════

STORE = simstore.SimStore(REPO)
OUTPUT = os.path.join(REPO, "output")


def _when(path: str) -> datetime:
    return datetime.fromtimestamp(os.path.getmtime(path))


def _fmt(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "—"


@dataclass
class Sim:
    model: str
    experiment: str
    variant: str
    stamp: str
    dir: str
    meta: dict
    mtime: datetime

    @property
    def run_id(self) -> str:
        return self.meta.get("run_id", "?")

    @property
    def newest(self) -> bool:
        """Is this the vintage an id resolves to today? Older ones are kept (they are the
        audit trail) but nothing reads them unless _SIM_STAMP pins one."""
        return self._newest

    _newest: bool = False


def simulations() -> list[Sim]:
    rows: list[Sim] = []
    for v in STORE.inventory():
        f = v.file()
        path = f if os.path.isfile(f) else v.dir
        rows.append(Sim(v.model, v.experiment, v.variant, v.stamp, v.dir,
                        v.meta(), _when(path)))
    # Newest per family, computed once here rather than re-resolved per deck below.
    seen: set[tuple[str, str, str]] = set()
    for r in sorted(rows, key=lambda r: r.stamp, reverse=True):
        key = (r.model, r.experiment, r.variant)
        if key not in seen:
            r._newest = True
            seen.add(key)
    return sorted(rows, key=lambda r: r.stamp, reverse=True)


@dataclass
class Source:
    """One run a deck overlays, and how it stands against the deck's pictures."""
    run_id: str
    label: str
    path: str | None
    stamp: str | None
    mtime: datetime | None
    newer_than_plot: bool = False


@dataclass
class Deck:
    name: str
    dir: str
    kind: str
    pngs: list[str]
    drawn: datetime | None          # OLDEST png — a deck is only as fresh as its stalest panel
    newest_png: datetime | None
    sources: list[Source] = field(default_factory=list)

    @property
    def stale(self) -> list[Source]:
        return [s for s in self.sources if s.newer_than_plot]

    @property
    def missing(self) -> list[Source]:
        return [s for s in self.sources if s.path is None]

    @property
    def badge(self) -> str:
        if self.kind == "unmapped":
            return "?"
        if self.stale:
            return "STALE"
        if self.missing:
            return "NO EXPORT"
        return "ok"


def decks(sims: list[Sim]) -> list[Deck]:
    """Every folder under output/ that holds PNGs, with the runs it overlays resolved the
    way plot_runs.jl would resolve them right now.

    THE COMPARISON IS AGAINST THE OLDEST PNG, not the newest. A deck is re-rendered as a
    set, but a panel that failed to redraw keeps its old file — and it is that panel, the
    stalest one, that decides whether the FOLDER can be trusted.
    """
    by_family: dict[tuple[str, str, str], Sim] = {
        (s.model, s.experiment, s.variant): s for s in sims if s.newest
    }
    out: list[Deck] = []
    if not os.path.isdir(OUTPUT):
        return out
    for name in sorted(os.listdir(OUTPUT)):
        d = os.path.join(OUTPUT, name)
        if not os.path.isdir(d):
            continue
        pngs = sorted(f for f in os.listdir(d) if f.endswith(".png"))
        if not pngs:
            continue
        times = [_when(os.path.join(d, f)) for f in pngs]
        kind = deck_kind(name)
        deck = Deck(name, d, kind, pngs, min(times), max(times))

        def add(rid: str, label: str, sim: Sim | None):
            if sim is None:
                deck.sources.append(Source(rid, label, None, None, None))
                return
            s = Source(rid, label, sim.dir, sim.stamp, sim.mtime)
            s.newer_than_plot = deck.drawn is not None and sim.mtime > deck.drawn
            deck.sources.append(s)

        if kind == "analysis":
            exp = experiment_of(name)
            for rid in models_of(name):
                row = REGISTRY.get(rid)
                if row is None:                  # an id the registry no longer carries
                    add(rid, "(not in registry)", None)
                else:
                    add(rid, row.label, by_family.get((row.model, exp, row.variant)))
        elif kind == "sweep":
            exp, variant = sweep_family(name)
            # Every model that HAS this sweep family — the deck name does not say which
            # model ran it, so anything matching (experiment, variant) is a source.
            hits = [s for (m, e, v), s in by_family.items() if e == exp and v == variant]
            if not hits:
                add(variant or "sweep", f"{exp} / {variant or '?'}", None)
            for s in sorted(hits, key=lambda s: s.model):
                add(f"{s.model}:{variant}", f"{s.model} {variant}", s)

        out.append(deck)
    return out


# ══════════════════════════════════════════════════════════════════════════════
# THE COMMAND
# ══════════════════════════════════════════════════════════════════════════════

def build_command(model: str, deck: str, run_ids: list[str], extra: str,
                  plot: bool) -> tuple[list[str], dict[str, str], str]:
    """(argv, env-overlay, the copy-pasteable line). One place, so what the window SHOWS
    and what it RUNS cannot drift — the shown text is rendered from the same dict."""
    env = {"_MODEL": model, "_DECK": deck}
    if run_ids:
        env["_RUN_IDS"] = ",".join(run_ids)
    if not plot:
        env["_NOPLOT"] = "1"
    # Free-form `K=V K=V`, applied last so it can override anything above — including
    # _MODEL, which is occasionally what you want.
    for tok in extra.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            env[k if k.startswith("_") else "_" + k] = v
    argv = ["julia", "--project=.", "main.jl"]
    shown = " ".join(f"{k}={v}" for k, v in env.items()) + " " + " ".join(argv)
    return argv, env, shown


# ══════════════════════════════════════════════════════════════════════════════
# --report
# ══════════════════════════════════════════════════════════════════════════════

def report() -> int:
    sims = simulations()
    print(f"\nSIMULATIONS — {len(sims)} vintages under {STORE.root}\n")
    for s in sims:
        mark = "*" if s.newest else " "
        fam = f"{s.model}/{s.experiment}/{s.variant}"
        print(f" {mark} {s.stamp}  {fam:<46} "
              f"id={s.run_id:<10} t_end={s.meta.get('t_end', '?')}")
    print("\n   * = the vintage this family resolves to today\n")

    ds = decks(sims)
    print(f"PLOTS — {len(ds)} deck folders under {OUTPUT}\n")
    bad = 0
    for d in ds:
        bad += bool(d.kind != "unmapped" and (d.stale or d.missing))
        print(f"  [{d.badge:^9}] {d.name}   {len(d.pngs)} png   drawn {_fmt(d.drawn)}")
        for s in d.sources:
            note = ("  <-- NEWER THAN THE PLOT" if s.newer_than_plot else
                    "  <-- no export" if s.path is None else "")
            print(f"             {s.run_id:<10} {s.stamp or '—':<16} {_fmt(s.mtime)}{note}")
    print()
    return 1 if bad else 0


# ══════════════════════════════════════════════════════════════════════════════
# THE WINDOW
# ══════════════════════════════════════════════════════════════════════════════
# 8-bit, off brain.svg: near-black ground, the magenta ramp for everything structural, and
# the hot pink reserved for what you can ACT on. Block glyphs and a monospace face do the
# rest — tkinter has no pixel font, so the chunkiness has to come from flat fills, 2 px
# borders and hard edges rather than from type.

BG      = "#0d0d0d"   # brain.svg's glasses
PANEL   = "#160a12"
INK     = "#ff9ecd"   # bgr2 stop 0 — body text
DIM     = "#8c5a74"
ACCENT  = "#ff1493"   # the gear — interactive things only
ACCENT2 = "#ff80cb"   # the gear's hub
DEEP    = "#c2185b"   # bgr2 stop 1 — headings
EDGE    = "#880e4f"   # the sulci — borders and rules
WARN    = "#ffd166"
BAD     = "#ff4d5e"
GOOD    = "#7ee081"
MONO    = ("DejaVu Sans Mono", 10)
MONO_B  = ("DejaVu Sans Mono", 10, "bold")
TITLE_F = ("DejaVu Sans Mono", 15, "bold")


def main() -> int:
    if "--report" in sys.argv:
        return report()

    # --selftest builds the entire widget tree against the real store, renders one frame
    # and closes — so "it opens" is something CI or a reviewer can check without a human
    # looking at a screen. It deliberately stops short of mainloop and never spawns julia.
    selftest = "--selftest" in sys.argv

    import tkinter as tk

    sims = simulations()
    ds = decks(sims)

    root = tk.Tk()
    root.title("DECKBOARD")
    root.configure(bg=BG)
    root.geometry("1280x860")

    def frame(parent, **kw):
        return tk.Frame(parent, bg=PANEL, highlightbackground=EDGE,
                        highlightthickness=2, **kw)

    def heading(parent, text):
        tk.Label(parent, text=text, bg=PANEL, fg=DEEP, font=MONO_B,
                 anchor="w").pack(fill="x", padx=8, pady=(6, 2))

    # --- banner -------------------------------------------------------------
    top = tk.Frame(root, bg=BG)
    top.pack(fill="x", padx=10, pady=(10, 4))
    tk.Label(top, text="▛▀▘ DECKBOARD", bg=BG, fg=ACCENT, font=TITLE_F).pack(side="left")
    nstale = sum(1 for d in ds if d.kind != "unmapped" and d.stale)
    tk.Label(top,
             text=f"  {len(sims)} vintages · {len(ds)} decks · {nstale} stale",
             bg=BG, fg=DIM, font=MONO).pack(side="left", padx=10)

    body = tk.Frame(root, bg=BG)
    body.pack(fill="both", expand=True, padx=10, pady=4)
    body.columnconfigure(0, weight=3)
    body.columnconfigure(1, weight=2)
    body.rowconfigure(0, weight=1)

    # --- left: the two inventories -----------------------------------------
    left = tk.Frame(body, bg=BG)
    left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
    left.rowconfigure(0, weight=1)
    left.rowconfigure(1, weight=1)
    left.columnconfigure(0, weight=1)

    simbox = frame(left)
    simbox.grid(row=0, column=0, sticky="nsew", pady=(0, 6))
    heading(simbox, "█ SIMULATIONS  (newest first · ● = what resolves today)")
    simtxt = tk.Text(simbox, bg=BG, fg=INK, font=MONO, bd=0, height=12,
                     insertbackground=ACCENT, wrap="none")
    simtxt.pack(fill="both", expand=True, padx=8, pady=(0, 8))
    for tag, col in (("live", ACCENT2), ("old", DIM), ("hdr", DEEP)):
        simtxt.tag_configure(tag, foreground=col)
    for s in sims:
        simtxt.insert("end", f" {'●' if s.newest else '·'} {s.stamp}  ",
                      "live" if s.newest else "old")
        simtxt.insert("end", f"{s.model}/{s.experiment}/{s.variant}\n",
                      "live" if s.newest else "old")
        simtxt.insert("end", f"     id {s.run_id}   t_end {s.meta.get('t_end', '?')}"
                             f"   {s.meta.get('retcode', '?')}"
                             f"   deck {s.meta.get('deck', '?')}\n", "old")
    simtxt.configure(state="disabled")

    plotbox = frame(left)
    plotbox.grid(row=1, column=0, sticky="nsew")
    heading(plotbox, "█ PLOTS  (which runs they came from · staleness)")
    plottxt = tk.Text(plotbox, bg=BG, fg=INK, font=MONO, bd=0, height=12, wrap="none")
    plottxt.pack(fill="both", expand=True, padx=8, pady=(0, 8))
    for tag, col in (("ok", GOOD), ("stale", BAD), ("miss", WARN), ("dim", DIM)):
        plottxt.tag_configure(tag, foreground=col)
    def load_deck(d):
        """Fill the RERUN panel from a deck row. The model comes from the deck's OWN
        sources rather than being guessed: an m1_vs_m4 deck has two, and picking the one
        whose export is stale is the one you actually need to re-solve."""
        deck_entry.delete(0, "end")
        deck_entry.insert(0, d.name)
        want = d.stale or d.sources
        rows = [REGISTRY[s.run_id] for s in want if s.run_id in REGISTRY]
        if rows:
            model_var.set(rows[0].model[1:])
            rebuild_ids()
            for r in rows:
                if r.model == rows[0].model and r.id in id_vars:
                    id_vars[r.id].set(True)
        preview()

    for i, d in enumerate(ds):
        state = ("dim" if d.kind == "unmapped" else
                 "stale" if d.stale else "miss" if d.missing else "ok")
        tag = f"deck{i}"
        plottxt.tag_configure(tag, foreground=ACCENT2 if d.kind != "unmapped" else DIM)
        if d.kind != "unmapped":
            plottxt.tag_bind(tag, "<Button-1>", lambda _e, d=d: load_deck(d))
            plottxt.tag_bind(tag, "<Enter>", lambda _e: plottxt.configure(cursor="hand2"))
            plottxt.tag_bind(tag, "<Leave>", lambda _e: plottxt.configure(cursor=""))
        plottxt.insert("end", f" [{d.badge}] ", state)
        plottxt.insert("end", f"{d.name}\n", tag)
        plottxt.insert("end", f"     {len(d.pngs)} png · drawn {_fmt(d.drawn)}"
                              f" … {_fmt(d.newest_png)}\n", "dim")
        for s in d.sources:
            if s.path is None:
                plottxt.insert("end", f"     {s.run_id:<10} no export in the store\n", "miss")
            elif s.newer_than_plot:
                plottxt.insert("end", f"     {s.run_id:<10} {s.stamp}  "
                                      f"{_fmt(s.mtime)}  NEWER THAN THE PLOT\n", "stale")
            else:
                plottxt.insert("end", f"     {s.run_id:<10} {s.stamp}  "
                                      f"{_fmt(s.mtime)}\n", "dim")
    plottxt.configure(state="disabled")

    # --- right: the launcher ------------------------------------------------
    right = tk.Frame(body, bg=BG)
    right.grid(row=0, column=1, sticky="nsew")
    right.rowconfigure(1, weight=1)
    right.columnconfigure(0, weight=1)

    runbox = frame(right)
    runbox.grid(row=0, column=0, sticky="new", pady=(0, 6))
    heading(runbox, "█ RERUN")

    # Model buttons — the "clickable models". Selecting one filters the run ids below to
    # that family, because an id from another family is refused by the entry point anyway
    # (and that refusal is the guard that stops an M3 solve being filed under m1/).
    model_var = tk.StringVar(value="3")
    mrow = tk.Frame(runbox, bg=PANEL)
    mrow.pack(fill="x", padx=8, pady=2)
    tk.Label(mrow, text="MODEL", bg=PANEL, fg=DIM, font=MONO, width=8,
             anchor="w").pack(side="left")

    id_frame = tk.Frame(runbox, bg=PANEL)
    id_vars: dict[str, tk.BooleanVar] = {}

    def rebuild_ids(*_):
        for w in id_frame.winfo_children():
            w.destroy()
        id_vars.clear()
        fam = "m" + model_var.get()
        for rid in sorted(r.id for r in REGISTRY.values() if r.model == fam):
            v = tk.BooleanVar(value=False)
            id_vars[rid] = v
            tk.Checkbutton(id_frame, text=rid, variable=v, bg=PANEL, fg=INK,
                           selectcolor=BG, activebackground=PANEL, activeforeground=ACCENT,
                           font=MONO, bd=0, highlightthickness=0).pack(side="left")
        preview()

    for tag in ("1", "3", "4"):
        tk.Radiobutton(mrow, text=f" M{tag} ", value=tag, variable=model_var,
                       command=rebuild_ids, bg=PANEL, fg=ACCENT2, selectcolor=EDGE,
                       activebackground=EDGE, activeforeground=BG, font=MONO_B,
                       indicatoron=False, bd=0, width=5).pack(side="left", padx=2)

    tk.Label(runbox, text="RUN IDS", bg=PANEL, fg=DIM, font=MONO,
             anchor="w").pack(fill="x", padx=8, pady=(6, 0))
    id_frame.pack(fill="x", padx=8)

    def labelled_entry(text, default=""):
        r = tk.Frame(runbox, bg=PANEL)
        r.pack(fill="x", padx=8, pady=3)
        tk.Label(r, text=text, bg=PANEL, fg=DIM, font=MONO, width=8,
                 anchor="w").pack(side="left")
        e = tk.Entry(r, bg=BG, fg=INK, font=MONO, insertbackground=ACCENT,
                     bd=0, highlightbackground=EDGE, highlightthickness=1)
        e.pack(side="left", fill="x", expand=True)
        e.insert(0, default)
        e.bind("<KeyRelease>", lambda _e: preview())
        return e

    # "Name of output file" — the deck, which IS the output folder name and the prefix on
    # every PNG in it (<deck>__compare_<panel>.png).
    _mapped = [d for d in ds if d.kind == "analysis"]
    _default = next((d.name for d in _mapped if d.stale),
                    _mapped[0].name if _mapped else "postural_changes_m3_analysis")
    deck_entry = labelled_entry("DECK", _default)
    extra_entry = labelled_entry("EXTRA", "")
    tk.Label(runbox, text="         e.g.  _T_END=2000 _XLIM=0,2000 _REUSE=1"
                          "   (applied last — can override anything above)",
             bg=PANEL, fg=DIM, font=("DejaVu Sans Mono", 8), anchor="w").pack(fill="x", padx=8)

    plot_var = tk.BooleanVar(value=True)
    tk.Checkbutton(runbox, text=" draw the deck too (unset = _NOPLOT=1, export only)",
                   variable=plot_var, command=lambda: preview(), bg=PANEL, fg=INK,
                   selectcolor=BG, activebackground=PANEL, activeforeground=ACCENT,
                   font=MONO, bd=0, highlightthickness=0, anchor="w").pack(fill="x",
                                                                          padx=8, pady=(4, 0))

    cmd_lbl = tk.Label(runbox, text="", bg=BG, fg=ACCENT2, font=MONO, anchor="w",
                       justify="left", wraplength=430)
    cmd_lbl.pack(fill="x", padx=8, pady=6)

    warn_lbl = tk.Label(runbox, text="", bg=PANEL, fg=WARN, font=MONO, anchor="w",
                        justify="left", wraplength=430)
    warn_lbl.pack(fill="x", padx=8)

    state = {"argv": None, "env": None, "armed": False, "proc": None}

    def current():
        ids = [k for k, v in id_vars.items() if v.get()]
        return build_command(model_var.get(), deck_entry.get().strip(), ids,
                             extra_entry.get(), plot_var.get())

    def preview(*_):
        argv, env, shown = current()
        state.update(argv=argv, env=env, armed=False)
        cmd_lbl.configure(text=shown)
        run_btn.configure(text="▶ ARM", bg=EDGE)
        notes = []
        deck = deck_entry.get().strip()
        want = models_of(deck)
        chosen = [k for k, v in id_vars.items() if v.get()]
        # The deck draws a fixed id set; anything you solve outside it is stored but not
        # drawn, and anything in it you did NOT solve is drawn from an older vintage.
        if chosen and not set(chosen) & set(want):
            notes.append(f"none of {', '.join(chosen)} is drawn by {deck} "
                         f"(it overlays {', '.join(want)})")
        if plot_var.get():
            notes.append("this ends by uploading the deck's PNGs to Google Drive.")
        warn_lbl.configure(text="  ".join("⚠ " + n for n in notes))

    logbox = frame(right)
    logbox.grid(row=1, column=0, sticky="nsew")
    heading(logbox, "█ OUTPUT  (ctrl-c interrupts · type + enter sends to stdin)")
    log = tk.Text(logbox, bg=BG, fg=INK, font=MONO, bd=0, wrap="none",
                  insertbackground=ACCENT)
    log.pack(fill="both", expand=True, padx=8, pady=(0, 4))
    log.tag_configure("cmd", foreground=ACCENT)
    log.tag_configure("err", foreground=BAD)
    log.tag_configure("end", foreground=GOOD)

    # Terminal manners. Ctrl-C with a selection still copies (this is a window, not an
    # xterm); with NO selection it does what a terminal does — SIGINT to the solve's whole
    # process group, which julia takes as InterruptException, so a wedged solve dies
    # without taking the window with it.
    def interrupt(_e=None):
        p = state["proc"]
        if p is not None and p.poll() is None:
            lines.put(("^C\n", "err"))
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGINT)
            except ProcessLookupError:
                pass
        return "break"

    def log_ctrl_c(_e):
        if log.tag_ranges("sel"):
            return None                     # fall through to the default <<Copy>>
        return interrupt()

    log.bind("<Control-c>", log_ctrl_c)

    # stdin. Anything the solve asks on its terminal (a readline prompt, an overwrite
    # question) used to hang forever with no way to answer but killing the run.
    inrow = tk.Frame(logbox, bg=PANEL)
    inrow.pack(fill="x", padx=8, pady=(0, 8))
    tk.Label(inrow, text="❯", bg=PANEL, fg=ACCENT, font=MONO_B).pack(side="left")
    stdin_entry = tk.Entry(inrow, bg=BG, fg=INK, font=MONO, insertbackground=ACCENT,
                           bd=0, highlightbackground=EDGE, highlightthickness=1)
    stdin_entry.pack(side="left", fill="x", expand=True, padx=(6, 0))

    def send_stdin(_e=None):
        text = stdin_entry.get()
        stdin_entry.delete(0, "end")
        p = state["proc"]
        if p is None or p.poll() is not None or p.stdin is None:
            lines.put(("(nothing is running to receive input)\n", "err"))
            return
        lines.put((text + "\n", "cmd"))     # echo, the way a terminal would
        try:
            p.stdin.write(text + "\n")
            p.stdin.flush()
        except (BrokenPipeError, ValueError):
            lines.put(("(stdin is closed)\n", "err"))

    stdin_entry.bind("<Return>", send_stdin)
    stdin_entry.bind("<Control-c>", interrupt)

    def log_type(e):
        """The log is history, not a buffer — printable keys land in the input line."""
        if e.char and e.char.isprintable():
            stdin_entry.focus_set()
            stdin_entry.insert("end", e.char)
            return "break"
        return None

    log.bind("<Key>", log_type)

    lines: queue.Queue = queue.Queue()

    def pump():
        try:
            while True:
                text, tag = lines.get_nowait()
                log.insert("end", text, tag)
                log.see("end")
        except queue.Empty:
            pass
        root.after(120, pump)

    def spawn():
        """Two clicks: the first ARMS (shows exactly what will run), the second FIRES.
        A solve is minutes long and ends outward-facing, so a single stray click must not
        be able to start one."""
        if not state["armed"]:
            state["armed"] = True
            run_btn.configure(text="▶▶ CONFIRM — RUN IT", bg=ACCENT)
            lines.put(("\n$ " + current()[2] + "\n", "cmd"))
            return
        argv, env, shown = current()
        run_btn.configure(state="disabled", text="… RUNNING", bg=EDGE)

        def worker():
            e = dict(os.environ, **env)
            try:
                # Its own session, so Ctrl-C above can SIGINT the group (julia and anything
                # julia spawned) without touching deckboard's own process group.
                p = subprocess.Popen(argv, cwd=REPO, env=e, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, bufsize=1, start_new_session=True)
                state["proc"] = p
                for line in p.stdout:
                    lines.put((line, None))
                p.wait()
                lines.put((f"\n[exit {p.returncode}]\n", "end" if p.returncode == 0 else "err"))
            except FileNotFoundError:
                lines.put(("\njulia is not on PATH — start deckboard from a shell that has it.\n",
                           "err"))
            finally:
                state["proc"] = None
                root.after(0, lambda: run_btn.configure(state="normal"))
                root.after(0, preview)

        threading.Thread(target=worker, daemon=True).start()

    btns = tk.Frame(runbox, bg=PANEL)
    btns.pack(fill="x", padx=8, pady=8)
    run_btn = tk.Button(btns, text="▶ ARM", command=spawn, bg=EDGE, fg=BG,
                        activebackground=ACCENT, activeforeground=BG, font=MONO_B,
                        bd=0, relief="flat", padx=14, pady=6)
    run_btn.pack(side="left")

    def copy():
        root.clipboard_clear()
        root.clipboard_append(current()[2])
        lines.put(("\n(command copied to clipboard)\n", "end"))

    tk.Button(btns, text="COPY", command=copy, bg=PANEL, fg=ACCENT2,
              activebackground=EDGE, activeforeground=BG, font=MONO_B, bd=0,
              relief="flat", padx=14, pady=6, highlightbackground=EDGE,
              highlightthickness=1).pack(side="left", padx=6)

    def refresh():
        root.destroy()
        os.execv(sys.executable, [sys.executable] + sys.argv)

    tk.Button(btns, text="RESCAN", command=refresh, bg=PANEL, fg=DIM,
              activebackground=EDGE, activeforeground=BG, font=MONO_B, bd=0,
              relief="flat", padx=14, pady=6, highlightbackground=EDGE,
              highlightthickness=1).pack(side="right")

    rebuild_ids()
    if selftest:
        root.update_idletasks()
        root.update()
        argv, env, shown = current()
        print("deckboard --selftest: widget tree built and rendered")
        print(f"  {len(sims)} vintages · {len(ds)} decks · {nstale} stale")
        print(f"  default command: {shown}")
        assert argv[:1] == ["julia"] and "_DECK" in env, "command builder is wrong"
        # Exercise the click path too — a deck row is only useful if selecting it really
        # does load its model and its ids into the launcher.
        for d in ds:
            if d.kind == "analysis" and d.stale:
                load_deck(d)
                root.update()
                print(f"  clicked {d.name}:")
                print(f"    {current()[2]}")
                break
        root.destroy()
        return 0
    pump()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
