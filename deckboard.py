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

THE RUN GOES TO A NEW TERMINAL. The board does not own the process at all: it builds the
command, opens a terminal window and hands it over. A terminal already does scrollback,
ctrl-c and stdin better than a Text widget can, and — the actual point — you get as many as
you want, so the second thing you run does not queue behind the first. Nothing is piped
back here, so closing the board does not touch a solve that is still going.

THE EXPORTS ARE THE OTHER HALF OF THE WINDOW. Every .h5 the repo holds is listed with its
size and its vintage. Click one and the command becomes a REPLOT of that exact vintage
(_SIM_STAMP) rather than a re-solve — the store already holds the answer far more often
than not, and a solve is minutes where a replot is seconds. Deleting is here too, because a
sweep vintage is fifty 155 MB files in one folder and nothing else in the repo offers to
remove any of it.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
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


def open_folder(path: str) -> None:
    """Hand a deck's folder to the desktop's file manager.

    Detached and never awaited — the opener outlives this process and its exit code says
    nothing useful anyway. A failure (no desktop, headless session) is printed rather than
    raised: not being able to open a window is not a reason to take the board down.
    """
    if not os.path.isdir(path):
        print(f"deckboard: no such folder {path}", file=sys.stderr)
        return
    if sys.platform == "darwin":
        cmd = ["open", path]
    elif os.name == "nt":
        os.startfile(path)  # noqa: S606 — the Windows opener has no argv form
        return
    else:
        cmd = ["xdg-open", path]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError as e:
        print(f"deckboard: could not open {path} — {e}", file=sys.stderr)


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
# THE EXPORTS
# ══════════════════════════════════════════════════════════════════════════════
# One row per .h5 FILE, not per vintage. A sweep vintage is fifty 155 MB files in a single
# stamp folder, so "which points exist, and what do they cost" is a question the vintage
# list above — one line for the whole folder — cannot answer. It is also where the disk
# goes: the store runs to tens of gigabytes and nothing else here offers to delete any.

def _size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


@dataclass(frozen=True)
class Export:
    path: str
    model: str
    experiment: str
    variant: str
    stamp: str
    file: str
    size: int
    mtime: datetime
    meta: dict

    @property
    def family(self) -> str:
        return "/".join(p for p in (self.model, self.experiment, self.variant) if p)


def exports() -> list[Export]:
    """Every .h5 in the repo, newest vintage first.

    TWO ROOTS, because there are two kinds. The STORE
    (`simulations/<model>/<experiment>/<variant>/<stamp>/`) is where a solve files its
    exports and where the gigabytes are; its meta.json describes the whole vintage, so
    every file in one carries the same one. A stray .h5 under `output/` is a deck folder
    holding its own copy from before the store existed — listed rather than hidden,
    precisely because nothing else in the board accounts for it.
    """
    rows: list[Export] = []
    for v in STORE.inventory():
        meta = v.meta()
        for name in v.files():
            try:
                st = os.stat(v.file(name))
            except OSError:          # deleted from under us between listing and stat
                continue
            rows.append(Export(v.file(name), v.model, v.experiment, v.variant, v.stamp,
                               name, st.st_size, datetime.fromtimestamp(st.st_mtime), meta))
    for dirpath, _, files in os.walk(OUTPUT):
        for name in sorted(f for f in files if f.endswith(".h5")):
            p = os.path.join(dirpath, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            deck = os.path.relpath(dirpath, OUTPUT).split(os.sep)[0]
            rows.append(Export(p, "output", deck, "", "", name, st.st_size,
                               datetime.fromtimestamp(st.st_mtime), {}))
    return sorted(rows, key=lambda r: (r.stamp, r.family, r.file), reverse=True)


# ══════════════════════════════════════════════════════════════════════════════
# THE REPLOT JOBS
# ══════════════════════════════════════════════════════════════════════════════
# Pinning a vintage is only half of "use this instead of solving": the other half is the
# DRIVER, and every deck is drawn with a protocol (angle / pressure / static) that is not
# derivable from its name. That mapping already exists — plot_all_decks.jl's JOBS table is
# what a full replot run uses — so it is parsed, like the registry, rather than restated. A
# deck this cannot find gets no replot offered, instead of one drawn as the wrong protocol.

PLOT_ALL_JL = {n: os.path.join(REPO, f"Model_{n}", "plot_all_decks.jl")
               for n in ("3", "4")}


def _kwargs(s: str) -> dict[str, str]:
    return dict(re.findall(r'(\w+)\s*=\s*"([^"]*)"', s))


def parse_replot_jobs() -> dict[str, dict[str, dict]]:
    """deck folder -> {model: {kind, protocol, deck, sweep, ramp_target, beats_models}}.

    `kind` is which of the two builders the table used: a plain deck goes to
    replot_deck.jl, a sweep to main_sweep.jl with _REPLOT=1. The `deck` INSIDE a sweep
    entry is not the key — a sweep folder is drawn by the analysis deck it belongs to.

    KEYED BY MODEL, because there are two tables and they overlap. `postural_changes_k_sweep`
    is in both, each drawing it with its own analysis deck. And the model has to come from
    the TABLE rather than from the MODEL buttons: `postural_changes_m1_vs_m3` is an M1-and-M3
    deck that only Model_3 knows how to redraw, so choosing the driver by which model
    produced the .h5 would send it to a Model_1 replot driver that does not exist.
    """
    jobs: dict[str, dict[str, dict]] = {}
    for model, path in PLOT_ALL_JL.items():
        if not os.path.isfile(path):
            continue
        text = _uncommented(path)
        for kind, fn in (("deck", "replot_cmd"), ("sweep", "sweep_replot_cmd")):
            for m in re.finditer(r'"(\w+)"\s*=>\s*\[' + fn + r'\(;([^)]*)\)\]', text):
                jobs.setdefault(m.group(1), {})[model] = dict(
                    _kwargs(m.group(2)), kind=kind, model=model)
    if not jobs:
        raise SystemExit(f"deckboard: parsed no replot jobs from "
                         f"{', '.join(PLOT_ALL_JL.values())} — the JOBS table moved, fix "
                         f"parse_replot_jobs().")
    return jobs


REPLOT_JOBS = parse_replot_jobs()


def job_for(deck: str, model: str) -> dict | None:
    """This deck's replot job: the selected model's if that model draws it, otherwise the
    only model that claims it. Two claimants and neither selected is genuinely ambiguous —
    a sweep folder drawn by M3 and by M4 — so it returns None rather than picking."""
    jobs = REPLOT_JOBS.get(deck, {})
    if model in jobs:
        return jobs[model]
    return next(iter(jobs.values())) if len(jobs) == 1 else None


def replot_driver(job: dict) -> str | None:
    """The script that redraws without solving, if the job's model has one. M1 has no
    replot_deck.jl, and saying so is better than pinning a vintage onto an entry point that
    will cheerfully solve it again."""
    rel = f"Model_{job['model']}/" + ("main_sweep.jl" if job["kind"] == "sweep"
                                      else "replot_deck.jl")
    return rel if os.path.isfile(os.path.join(REPO, rel)) else None


# ══════════════════════════════════════════════════════════════════════════════
# HANDING IT TO A TERMINAL
# ══════════════════════════════════════════════════════════════════════════════
# In the order they are tried, each with the flag that means "the rest is the command".
# $TERMINAL wins if you set it, and is assumed to take -e — every one of these but
# gnome-terminal does.

TERMINALS = [("gnome-terminal", ["--"]), ("konsole", ["-e"]), ("xfce4-terminal", ["-x"]),
             ("mate-terminal", ["--"]), ("tilix", ["-e"]), ("terminator", ["-x"]),
             ("alacritty", ["-e"]), ("kitty", []), ("foot", []),
             ("wezterm", ["start", "--"]), ("x-terminal-emulator", ["-e"]),
             ("xterm", ["-e"])]


def terminal_script(argv: list[str], env: dict[str, str], title: str) -> str:
    """The shell the terminal is asked to run.

    The environment is written INTO the script rather than passed as the terminal's own:
    gnome-terminal and friends spawn the window from a long-lived server process, which
    does not reliably carry the calling process's environ. Written in, it is also exactly
    the line the COPY button hands you, so what runs is what you can paste.

    It ends on a `read`, so a run that dies in the first second leaves its traceback on the
    screen instead of closing the window it was printed in.
    """
    line = ("env " + " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items()) + " "
            + " ".join(shlex.quote(a) for a in argv))
    return (f"cd {shlex.quote(REPO)} || exit 1\n"
            f"printf '\\033]0;%s\\007' {shlex.quote(title)}\n"
            f"{line}\n"
            f"rc=$?\n"
            f"printf '\\n[exit %s]\\n' \"$rc\"\n"
            f"read -r -p 'press enter to close '\n")


def launch_in_terminal(argv: list[str], env: dict[str, str], title: str) -> str:
    """Open a terminal running the command; return which one. Raises if there is none."""
    script = terminal_script(argv, env, title)
    first = [(os.environ["TERMINAL"], ["-e"])] if os.environ.get("TERMINAL") else []
    for name, flag in first + TERMINALS:
        exe = shutil.which(name)
        if exe is None:
            continue
        # stderr is deliberately NOT swallowed: a terminal that refuses its own arguments
        # then says so on the shell deckboard was started from, which is a great deal
        # easier to work out than a window that simply never appears.
        subprocess.Popen([exe, *flag, "bash", "-lc", script], cwd=REPO,
                         start_new_session=True)
        return name
    raise RuntimeError("no terminal emulator found — set $TERMINAL to the one you use")


# ══════════════════════════════════════════════════════════════════════════════
# THE COMMAND
# ══════════════════════════════════════════════════════════════════════════════

def build_command(model: str, deck: str, run_ids: list[str], extra: str,
                  plot: bool, pin: str = "") -> tuple[list[str], dict[str, str], str]:
    """(argv, env-overlay, the copy-pasteable line). One place, so what the window SHOWS,
    what it RUNS and what COPY gives you cannot drift — all three render from this dict.

    WITH A PIN it is a DIFFERENT command, not the same one with a variable bolted on. A
    pinned vintage means "draw what is already on disk", and that is replot_deck.jl — or
    main_sweep.jl with _REPLOT=1 for a sweep — plus the deck's protocol out of the JOBS
    table. main.jl would accept _SIM_STAMP and then solve anyway, which is the one outcome
    a pin is chosen to avoid. When the model has no replot driver, or the deck is not in
    the table, it falls back to main.jl carrying the pin and the window says plainly that
    this will still solve.
    """
    job = job_for(deck, model) if pin else None
    driver = replot_driver(job) if job else None
    if pin and job and driver:
        env = {"_SIM_STAMP": pin, "_PROTOCOL": job.get("protocol", "angle"),
               # A sweep folder is drawn by the analysis deck it belongs to, so the _DECK
               # here is the table's, not the folder you clicked.
               "_DECK": job.get("deck", deck)}
        if job["kind"] == "sweep":
            env.update(_SWEEP=job.get("sweep", ""), _REPLOT="1", _SIM_MODE="1",
                       _RAMP_TARGET=job.get("ramp_target", "Theta"))
        elif job.get("beats_models"):
            env["_BEATS_MODELS"] = job["beats_models"]
        argv = ["julia", "--project=.", driver]
        return _finish(env, argv, extra)
    env = {"_MODEL": model, "_DECK": deck}
    if pin:
        env["_SIM_STAMP"] = pin
    if run_ids:
        env["_RUN_IDS"] = ",".join(run_ids)
    if not plot:
        env["_NOPLOT"] = "1"
    # Free-form `K=V K=V`, applied last so it can override anything above — including
    # _MODEL, which is occasionally what you want.
    return _finish(env, ["julia", "--project=.", "main.jl"], extra)


def _finish(env: dict[str, str], argv: list[str], extra: str):
    for tok in extra.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            env[k if k.startswith("_") else "_" + k] = v
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

BRAIN_SVG = os.path.join(REPO, "brain.svg")


def _quad_points(d: str) -> list[tuple[float, float]]:
    """An SVG path as a polyline. ABSOLUTE M / Q / Z only — which is all brain.svg uses —
    with each quadratic sampled into eight segments. Anything else returns nothing at all,
    so an unfamiliar path is skipped rather than drawn wrong."""
    toks = re.findall(r"[A-Za-z]|-?[\d.]+", d)
    out: list[tuple[float, float]] = []
    cur = (0.0, 0.0)
    i = 0
    while i < len(toks):
        c, i = toks[i], i + 1
        if c == "M" and i + 1 < len(toks):
            cur = (float(toks[i]), float(toks[i + 1]))
            i += 2
            out.append(cur)
        elif c == "Q" and i + 3 < len(toks):
            cx, cy, x, y = (float(toks[i + k]) for k in range(4))
            i += 4
            for k in range(1, 9):
                t, mt = k / 8, 1 - k / 8
                out.append((mt * mt * cur[0] + 2 * mt * t * cx + t * t * x,
                            mt * mt * cur[1] + 2 * mt * t * cy + t * t * y))
            cur = (x, y)
        elif c in "Zz":
            continue
        else:
            return []
    return out


def draw_brain(canvas, size: int) -> None:
    """brain.svg itself, in the corner of the banner.

    The palette above was sampled from this file — the ground is its glasses, the rules are
    its sulci, the hot pink is its gear — so the mark it was taken from belongs here, small
    and in those same colours rather than its own pinks.

    DRAWS NOTHING ON ANY SURPRISE. It is decoration: a missing file, an unfamiliar path
    command or a viewBox that moved must cost an empty corner, never the window."""
    try:
        svg = open(BRAIN_SVG, encoding="utf-8").read()
        s = size / 64.0                      # brain.svg is viewBox="0 0 64 64"
        for tag in re.findall(r"<path[^>]*>", svg):
            d = re.search(r'\bd="([^"]+)"', tag)
            pts = _quad_points(d.group(1)) if d else []
            if len(pts) < 2:
                continue
            flat = [c * s for p in pts for c in p]
            if 'fill="none"' in tag:         # a sulcus
                canvas.create_line(*flat, fill=DEEP, width=1)
            else:                            # the cortex itself
                canvas.create_polygon(*flat, fill=EDGE, outline="")
        for cx, cy, r in re.findall(
                r'<circle[^>]*cx="([\d.]+)"[^>]*cy="([\d.]+)"[^>]*r="([\d.]+)"', svg):
            x, y, rr = float(cx) * s, float(cy) * s, float(r) * s
            canvas.create_oval(x - rr, y - rr, x + rr, y + rr, outline=DEEP)
        for x1, y1, x2, y2 in re.findall(
                r'<line[^>]*x1="([\d.]+)"[^>]*y1="([\d.]+)"[^>]*x2="([\d.]+)"[^>]*y2="([\d.]+)"',
                svg):
            canvas.create_line(float(x1) * s, float(y1) * s, float(x2) * s, float(y2) * s,
                               fill=DEEP)
    except Exception:                        # decoration: never fatal, never loud
        pass


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

    # className is what lands in WM_CLASS, and WM_CLASS is how the dock decides whether the
    # window belongs to the pinned launcher or deserves an icon of its own. Left at the
    # default every tkinter program on the machine is "Tk" and the brain icon does not match.
    root = tk.Tk(className="Deckboard")
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
    glyph = tk.Canvas(top, width=26, height=26, bg=BG, highlightthickness=0, bd=0)
    glyph.pack(side="left", padx=(0, 8))
    draw_brain(glyph, 26)
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
    heading(plotbox, "█ PLOTS  (click a name to load it · ↗ opens its folder)")
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
        # Every deck gets the opener, unmapped ones included — "what is actually in this
        # folder" is exactly the question an unmapped folder raises, and it is the one
        # thing this window cannot answer for it.
        otag = f"open{i}"
        plottxt.tag_configure(otag, foreground=ACCENT)
        plottxt.tag_bind(otag, "<Button-1>", lambda _e, p=d.dir: open_folder(p))
        plottxt.tag_bind(otag, "<Enter>", lambda _e: plottxt.configure(cursor="hand2"))
        plottxt.tag_bind(otag, "<Leave>", lambda _e: plottxt.configure(cursor=""))
        plottxt.insert("end", f" [{d.badge}] ", state)
        plottxt.insert("end", f"{d.name}", tag)
        plottxt.insert("end", "  ↗\n", otag)
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

    # The launcher's state. No process lives here: a run is handed to a terminal and
    # forgotten about. "pin" is the vintage clicked in EXPORTS below, and it changes WHICH
    # command gets built rather than just adding a variable to the one that was there.
    state = {"argv": None, "env": None, "armed": False,
             "pin": "", "sel": None, "seltag": "", "del_armed": False}

    def current():
        ids = [k for k, v in id_vars.items() if v.get()]
        return build_command(model_var.get(), deck_entry.get().strip(), ids,
                             extra_entry.get(), plot_var.get(), state["pin"])

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
        # drawn, and anything in it you did NOT solve is drawn from an older vintage. A
        # replot has no say in what it solves, so the note is about solving only.
        if chosen and not state["pin"] and not set(chosen) & set(want):
            notes.append(f"none of {', '.join(chosen)} is drawn by {deck} "
                         f"(it overlays {', '.join(want)})")
        # A pin that cannot reach a replot driver is the one case where the button does the
        # OPPOSITE of what clicking an export means, so it is said plainly rather than left
        # to be discovered when the terminal starts solving.
        if state["pin"]:
            job = job_for(deck, model_var.get())
            if job is None:
                notes.append(f"no replot job for {deck} in plot_all_decks.jl (pick the "
                             f"model if two claim it) — pinned, but this will still SOLVE")
            elif replot_driver(job) is None:
                notes.append(f"M{job['model']} has no replot driver — the vintage is "
                             f"pinned, but this will still SOLVE")
        if plot_var.get() or state["pin"]:
            notes.append("this ends by uploading the deck's PNGs to Google Drive.")
        warn_lbl.configure(text="  ".join("⚠ " + n for n in notes))

    # --- the pin, and where the last run went -------------------------------
    pinrow = tk.Frame(runbox, bg=PANEL)
    pinrow.pack(fill="x", padx=8, pady=(4, 0))
    tk.Label(pinrow, text="PIN", bg=PANEL, fg=DIM, font=MONO, width=8,
             anchor="w").pack(side="left")
    NOPIN = "none — newest vintage of each run"
    pin_lbl = tk.Label(pinrow, text=NOPIN, bg=PANEL, fg=DIM, font=MONO, anchor="w")
    pin_lbl.pack(side="left")

    def clear_pin(*_):
        if state["seltag"]:
            xtxt.tag_configure(state["seltag"], foreground=INK)
        state.update(pin="", sel=None, seltag="", del_armed=False)
        pin_lbl.configure(text=NOPIN, fg=DIM)
        del_btn.configure(text="✕ DELETE", bg=PANEL, fg=BAD)
        sel_lbl.configure(text=SUMMARY, fg=DIM)
        preview()

    tk.Button(pinrow, text="CLEAR", command=clear_pin, bg=PANEL, fg=ACCENT2,
              activebackground=EDGE, activeforeground=BG, font=MONO, bd=0, relief="flat",
              padx=6).pack(side="right")

    # The only thing the board still says about a run: which window it went to. Everything
    # else about it is in that window.
    launch_lbl = tk.Label(runbox, text="", bg=PANEL, fg=GOOD, font=MONO, anchor="w",
                          justify="left", wraplength=430)
    launch_lbl.pack(fill="x", padx=8, pady=(4, 0))

    def spawn():
        """Two clicks, unchanged. What the second one costs is now a window rather than a
        wait — but a deck run still ends by uploading its PNGs, so a single stray click
        must not be able to start one."""
        if not state["armed"]:
            state["armed"] = True
            run_btn.configure(text="▶▶ CONFIRM — OPEN A TERMINAL", bg=ACCENT)
            return
        argv, env, shown = current()
        state["armed"] = False
        run_btn.configure(text="▶ ARM", bg=EDGE)
        deck = env.get("_DECK", "run")
        try:
            term = launch_in_terminal(argv, env, "deckboard: " + deck)
        except RuntimeError as e:
            launch_lbl.configure(text=f"✕ {e}", fg=BAD)
            return
        launch_lbl.configure(text=f"→ {datetime.now():%H:%M:%S}  {deck} — "
                                  f"a new {term} window has it", fg=GOOD)

    # ── EXPORTS ──────────────────────────────────────────────────────────────
    # Where the live output used to be. The output is in a terminal now, and the question
    # this pane answers instead is the one that comes BEFORE a run: is the answer already
    # on disk? Clicking a file pins its vintage, and the command above turns into a replot
    # of it — seconds, against the minutes a re-solve costs.
    xbox = frame(right)
    xbox.grid(row=1, column=0, sticky="nsew")
    heading(xbox, "█ EXPORTS  (click an .h5 to replot that vintage instead of solving)")
    xtxt = tk.Text(xbox, bg=BG, fg=INK, font=MONO, bd=0, wrap="none")
    xtxt.pack(fill="both", expand=True, padx=8, pady=(0, 4))
    for tag, col in (("hdr", DEEP), ("dim", DIM)):
        xtxt.tag_configure(tag, foreground=col)

    XS = exports()
    SUMMARY = f"{len(XS)} files · {_size(sum(x.size for x in XS))} on disk"

    def select(x, tag):
        """Pin this vintage AND move the launcher onto it: the model comes from the family
        the file is filed under and the deck from the vintage's own meta.json, so what
        appears is a command that would really redraw THIS file — not one you then have
        to fix up by hand."""
        if state["seltag"]:
            xtxt.tag_configure(state["seltag"], foreground=INK)
        xtxt.tag_configure(tag, foreground=ACCENT2)
        state.update(sel=x, seltag=tag, del_armed=False, pin=x.stamp)
        del_btn.configure(text="✕ DELETE", bg=PANEL, fg=BAD)
        if x.model[1:] in ("1", "3", "4") and x.model.startswith("m"):
            model_var.set(x.model[1:])
            rebuild_ids()
        if x.meta.get("deck"):
            deck_entry.delete(0, "end")
            deck_entry.insert(0, x.meta["deck"])
        # An output/ .h5 predates the store and has no vintage to pin. It is listed so it
        # can be SEEN and deleted, and the label says why it will not pin.
        pin_lbl.configure(text=(f"{x.stamp} · {x.file}" if x.stamp else
                                f"{x.file} — no vintage to pin"),
                          fg=ACCENT2 if x.stamp else WARN)
        sel_lbl.configure(text=f"{os.path.relpath(x.path, REPO)}  {_size(x.size)}  "
                               f"{_fmt(x.mtime)}", fg=DIM)
        preview()

    last = None
    for i, x in enumerate(XS):
        if (x.stamp, x.family) != last:
            last = (x.stamp, x.family)
            m = x.meta
            bits = "  ".join(b for b in (f"t_end {m['t_end']}" if "t_end" in m else "",
                                         m.get("retcode", ""),
                                         f"deck {m['deck']}" if "deck" in m else "") if b)
            xtxt.insert("end", f"\n {x.stamp or '(no vintage)':<16}{x.family}\n", "hdr")
            if bits:
                xtxt.insert("end", f"      {bits}\n", "dim")
        tag = f"x{i}"
        xtxt.tag_configure(tag, foreground=INK)
        xtxt.tag_bind(tag, "<Button-1>", lambda _e, x=x, t=tag: select(x, t))
        xtxt.tag_bind(tag, "<Enter>", lambda _e: xtxt.configure(cursor="hand2"))
        xtxt.tag_bind(tag, "<Leave>", lambda _e: xtxt.configure(cursor=""))
        xtxt.insert("end", f"   {x.file}", tag)
        xtxt.insert("end", f"   {_size(x.size)}  {_fmt(x.mtime)}\n", "dim")
    xtxt.configure(state="disabled")

    foot = tk.Frame(xbox, bg=PANEL)
    foot.pack(fill="x", padx=8, pady=(0, 8))
    sel_lbl = tk.Label(foot, text=SUMMARY, bg=PANEL, fg=DIM, font=MONO, anchor="w",
                       justify="left", wraplength=360)
    sel_lbl.pack(side="left", fill="x", expand=True)

    def delete():
        """Two clicks, like ARM, and for a harder reason: 155 MB of solve does not come
        back. The confirming click is a DIFFERENT button — red, and naming the file and
        its size — so it cannot be the same reflex that armed it."""
        x = state["sel"]
        if x is None:
            sel_lbl.configure(text="click a file in the list first", fg=WARN)
            return
        if not state["del_armed"]:
            state["del_armed"] = True
            del_btn.configure(text=f"✕✕ CONFIRM DELETE {x.file} ({_size(x.size)})",
                              bg=BAD, fg=BG)
            return
        state["del_armed"] = False
        del_btn.configure(text="✕ DELETE", bg=PANEL, fg=BAD)
        try:
            os.remove(x.path)
        except OSError as e:
            sel_lbl.configure(text=f"could not delete: {e}", fg=BAD)
            return
        tag = state["seltag"]
        if state["pin"] == x.stamp:
            clear_pin()          # the pinned vintage may no longer hold what it promised
        state.update(sel=None, seltag="")
        # Struck through rather than removed: what WAS there is worth still being able to
        # read. RESCAN rebuilds both lists for real.
        xtxt.tag_configure(tag, foreground=EDGE, overstrike=True)
        sel_lbl.configure(text=f"deleted {x.file} — {_size(x.size)} freed · "
                               f"RESCAN to rebuild the lists", fg=GOOD)

    del_btn = tk.Button(foot, text="✕ DELETE", command=delete, bg=PANEL, fg=BAD,
                        activebackground=BAD, activeforeground=BG, font=MONO_B, bd=0,
                        relief="flat", padx=12, pady=4, highlightbackground=EDGE,
                        highlightthickness=1)
    del_btn.pack(side="right")

    btns = tk.Frame(runbox, bg=PANEL)
    btns.pack(fill="x", padx=8, pady=8)
    run_btn = tk.Button(btns, text="▶ ARM", command=spawn, bg=EDGE, fg=BG,
                        activebackground=ACCENT, activeforeground=BG, font=MONO_B,
                        bd=0, relief="flat", padx=14, pady=6)
    run_btn.pack(side="left")

    def copy():
        root.clipboard_clear()
        root.clipboard_append(current()[2])
        launch_lbl.configure(text="(command copied to clipboard)", fg=ACCENT2)

    tk.Button(btns, text="COPY", command=copy, bg=PANEL, fg=ACCENT2,
              activebackground=EDGE, activeforeground=BG, font=MONO_B, bd=0,
              relief="flat", padx=14, pady=6, highlightbackground=EDGE,
              highlightthickness=1).pack(side="left", padx=6)

    def refresh():
        # Free to do at any time now: no run belongs to this process, so restarting it
        # costs nothing but the window. This is also how a delete leaves the lists.
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
        # The exports half, and the pinned command it builds. Nothing is launched: the
        # selftest must never open a terminal, let alone start a solve.
        print(f"  {len(XS)} exports · {_size(sum(x.size for x in XS))}")
        pinnable = next((x for x in XS if x.stamp and x.meta.get("deck") in REPLOT_JOBS),
                        None)
        if pinnable is not None:
            select(pinnable, "x%d" % XS.index(pinnable))
            argv, env, shown = current()
            print(f"  clicked {pinnable.stamp}/{pinnable.file}:")
            print(f"    {shown}")
            assert env.get("_SIM_STAMP") == pinnable.stamp, "the pin did not reach the env"
            assert "replot" in argv[-1] or "sweep" in argv[-1], \
                "a pinned command must not be the solving entry point"
            script = terminal_script(argv, env, "deckboard: t")
            assert "_SIM_STAMP" in script and "read -r -p" in script, "bad terminal script"
            clear_pin()
            assert current()[0][-1] == "main.jl", "clearing the pin did not restore main.jl"
        print(f"  terminals available: "
              f"{[n for n, _ in TERMINALS if shutil.which(n)] or 'NONE — set $TERMINAL'}")
        root.destroy()
        return 0
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
