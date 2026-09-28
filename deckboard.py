#!/usr/bin/env python3
"""deckboard.py — what has been solved, what has been drawn, and which is stale.

    python3 deckboard/deckboard.py            # the window (needs tkinter — see below)
    python3 deckboard/deckboard.py --report   # the same data as text, no window
    python3 deckboard/deckboard.py --selftest # build the window, render one frame, exit
    python3 deckboard/deckboard.py --repo PATH ...   # read another checkout

WHERE IT LIVES. Deckboard is its own repository, checked out as a git submodule at
`deckboard/` in the brainphysio repo (human_m123), so a `git clone --recursive` brings it
and a plain clone does not. It reads the checkout it sits in — the folder above its own —
unless `--repo PATH` or $DECKBOARD_REPO names another one.

The window is stdlib, but on Debian/Ubuntu tkinter is a SEPARATE PACKAGE and the stock
`python3` does not have it: `sudo apt install python3-tk`, or use an interpreter that
already ships it (`~/anaconda3/bin/python3`). --report and --selftest below say which.

WHY THIS EXISTS. The store and the deck folders answer different halves of one question and
neither can answer it alone. `simulations/<model>/<experiment>/<variant>/<stamp>/run.h5` says
what has been SOLVED; `modelplots/<deck>/*.png` says what has been DRAWN. Nothing on disk joins
them — the deck's provenance stamp is only written when `_STAMP_OUT` is set, and it is
deliberately kept out of `modelplots/` so it cannot be committed. So "is this figure still
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

A PICTURE GOES OUT OF DATE TWO WAYS, and only one of them is the exports. The other is the
DRAWING CODE: plotting.jl, the panels, the registry's labels and colours, and the deck
folder's own layout.jl. Edit any of them and every PNG already on disk is a rendering of
the previous version, with the same run behind it — a staleness the export timestamps
cannot see, and the cheap one to fix, because it is a replot rather than a solve. So each
deck also carries the closure of the sources that DRAW it, walked from the include graph of
the replot driver the JOBS table names for it, and the badge says REPLOT when they are the
only thing that moved.

WHICH BRANCH YOU ARE ON is asked once, above the run button, because a run ends by putting
PNGs on Google Drive and nothing in that upload records the checkout that drew them.

NO NEW DEPENDENCIES — tkinter, subprocess and the stdlib, plus simstore.py from the repo
root. It is a .py rather than a notebook because it has to own a window and a subprocess for
the length of a solve, which a notebook cell does not do well; `--report` is there for when
you want the numbers in a terminal or a notebook instead.

WHAT IS SOLVED, NOT JUST WHICH DECK. A deck folder is `<experiment>_<suffix>` and the
experiment is DERIVED in main.jl from (`_PROTOCOL`, `_RAMP_TARGET`) — so those two are not
decoration on the command, they are what decides the folder the H5 is written to. The board
used to send neither, which is not "unset" but `angle`/`Theta`: every deck it launched
solved a postural tilt, and a `jugular_compression_*` deck then drew itself from whichever
pressure exports happened to already be on disk. Both are selectors now, filled in FROM the
deck through EXPERIMENT_FOR (parsed, like everything else here) and warned about when they
disagree with it. `_RAMP_AMP` sits beside them: one number for how far the ramp goes —
degrees under :angle, mmHg under :pressure — because amplitude was the one thing about a
protocol you had to edit Protocol.jl to change.

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

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime

def _repo() -> str:
    """The checkout this board reads: `--repo PATH`, else $DECKBOARD_REPO, else the folder
    above this file — which, with Deckboard checked out as the `deckboard/` submodule, is
    the brainphysio repo it was cloned into. RESCAN re-execs with the same argv, so a
    --repo survives it."""
    for i, a in enumerate(sys.argv):
        if a == "--repo" and i + 1 < len(sys.argv):
            return os.path.abspath(sys.argv[i + 1])
        if a.startswith("--repo="):
            return os.path.abspath(a.split("=", 1)[1])
    return os.path.abspath(os.environ.get("DECKBOARD_REPO") or
                           os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


REPO = _repo()
if not os.path.isfile(os.path.join(REPO, "simstore.py")):
    raise SystemExit(f"deckboard: {REPO} is not a brainphysio checkout (no simstore.py).\n"
                     "  Run it from the submodule — python3 deckboard/deckboard.py — or "
                     "point it at one with --repo PATH.")
sys.path.insert(0, REPO)
import simstore  # noqa: E402  — the store reader, already written and already correct


# ══════════════════════════════════════════════════════════════════════════════
# THE MODEL FOLDERS
# ══════════════════════════════════════════════════════════════════════════════
# The one piece of the layout stated here rather than parsed. The registry names runs by
# model id — m1, m3, m4 … — and nothing in the Julia says which folder a model id's sources
# live in. Since the rename the two no longer line up: m3 is Model_3_celia, m4 is Model_3,
# and the fitted models sit under fits/. Every folder has to exist or the board does not
# open, because a missing one means this is not a checkout of the human family, and a
# guessed folder is a solve sent to the wrong main.jl.

MODEL_DIRS = {
    "1": "Model_1",
    "3": "Model_3_celia",
    "4": "Model_3",
    "5": "fits/m3/Model_5",
    "6": "fits/m3/Model_6",
    "7": "fits/m1/Model_7",
}

# How each model SOLVES. Every model but M7 has a main.jl. M7's runs come out of
# ude_cs.jl's export stage, which solves the fitted M1 with its stock and its learned
# spinal law for one patient — all four of its registry rows at once — and draws nothing
# (fits/m1/Model_7/README.md). Its deck is then drawn like every patient deck, below.
SOLVE_ENTRY = {m: "main.jl" for m in MODEL_DIRS} | {"7": "ude_cs.jl"}
SOLVE_FLAGS = {"7": ["-t", "auto"]}
SOLVE_ENV = {"7": {"_STAGE": "export"}}

_absent = [d for d in MODEL_DIRS.values() if not os.path.isdir(os.path.join(REPO, d))]
if _absent:
    raise SystemExit(f"deckboard: {REPO} has no {', '.join(_absent)}.\n"
                     "  Deckboard reads the human family's layout (M1–M7, branch "
                     "human_m123); this checkout is something else.")


# ══════════════════════════════════════════════════════════════════════════════
# READING THE REPO
# ══════════════════════════════════════════════════════════════════════════════
# Three facts live in Julia and are parsed out of it rather than duplicated here. Each
# parser raises if it finds nothing: a regex that silently stops matching would leave this
# window showing an empty, confident, wrong picture — worse than failing to open.

REGISTRY_JL = os.path.join(REPO, "plotter", "registry.jl")
PLOTTING_JL = os.path.join(REPO, "plotter", "plotting.jl")
PROTOCOL_JL = os.path.join(REPO, "Protocol.jl")
MAIN_JL = [os.path.join(REPO, MODEL_DIRS[n], "main.jl") for n in ("3", "4", "5", "6", "1")]


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
    """One MODEL_REGISTRY entry. `variant_tpl` and `label_tpl` carry `{patient}` where
    registry.jl interpolates PATIENT_TAG: an M5 row is not one store family but one per
    recording, and which one a deck overlays depends on the deck (see patient_of)."""
    id: str
    model: str
    variant_tpl: str
    label_tpl: str
    color: str

    @property
    def per_patient(self) -> bool:
        return "{patient}" in self.variant_tpl

    def variant(self, patient: str = "") -> str:
        return self.variant_tpl.replace("{patient}", patient or DEFAULT_PATIENT)

    def label(self, patient: str = "") -> str:
        return self.label_tpl.replace("{patient}", patient or DEFAULT_PATIENT)


_TAG_RX = re.compile(r"\$\(?PATIENT_TAG\)?")


def _split_concat(expr: str) -> list[str]:
    """`a * b * c` at the top level — not inside a string or a call."""
    parts, depth, quoted, cur = [], 0, False, ""
    for ch in expr:
        if ch == '"':
            quoted = not quoted
        elif not quoted and ch in "([":
            depth += 1
        elif not quoted and ch in ")]":
            depth -= 1
        if ch == "*" and not quoted and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    return parts + [cur]


def _variant_expr(expr: str) -> str:
    """A registry.jl variant EXPRESSION as a template. The forms it writes: a literal,
    interpolated or not (`"nonpulsatile_p$(PATIENT_TAG)_nnA"`), `_variant(puls[, :shunt])`,
    and bare `PATIENT_TAG`, joined by `*`. Anything else raises rather than being guessed
    at: a wrong variant is a deck resolved against a store family it never drew from."""
    out = []
    for part in (p.strip() for p in _split_concat(expr)):
        lit = re.fullmatch(r'"([^"]*)"', part)
        call = re.fullmatch(r"_variant\(\s*(true|false)\s*(?:,\s*:(\w+)\s*)?\)", part)
        if lit:
            out.append(_TAG_RX.sub("{patient}", lit.group(1)))
        elif call:
            out.append(("pulsatile" if call.group(1) == "true" else "nonpulsatile")
                       + _SHUNT_TAG[call.group(2) or "none"])
        elif part == "PATIENT_TAG":
            out.append("{patient}")
        else:
            raise SystemExit(f"deckboard: cannot read the variant expression `{expr}` in "
                             f"{REGISTRY_JL} — extend _variant_expr().")
    return "".join(out)


def parse_default_patient() -> str:
    """PATIENT_TAG's fallback — the recording a deck is drawn for when neither its name
    nor its deck.json says."""
    m = re.search(r'PATIENT_TAG\s*=.*?get\(ENV,\s*"_PATIENT",\s*"(\w+)"\)',
                  _uncommented(REGISTRY_JL))
    return m.group(1) if m else "081"


def parse_patient_models(src: str) -> set[str]:
    """Which models' `_row`s carry the patient: `model in ("m5", "m6") ? _m5_variant …`.
    A registry that tags some rows and whose selector this cannot read raises — reading it
    as "none" would resolve every M5 deck against a family that no longer exists."""
    m = re.search(r"model\s+in\s+\(([^)]*)\)\s*\?\s*_m5_variant", src)
    if m:
        return set(re.findall(r'"(\w+)"', m.group(1)))
    m = re.search(r'model\s*==\s*"(\w+)"\s*\?\s*_m5_variant', src)
    if m:
        return {m.group(1)}
    if "_m5_variant" in src:
        raise SystemExit(f"deckboard: {REGISTRY_JL} tags rows by patient but the selector "
                         "moved — fix parse_patient_models().")
    return set()


def parse_registry() -> dict[str, Row]:
    """MODEL_REGISTRY, in both of the forms registry.jl writes.

    `_row(model, puls, shunt, label, color)` for the ordinary rows, and the explicit
    named-tuple form the bistability basins use (their variant is not derived from
    pulsatile/shunt at all — the basin IS the variant).
    """
    src = _uncommented(REGISTRY_JL)
    rows: dict[str, Row] = {}
    tagged = parse_patient_models(src)

    for m in re.finditer(
        r':(\w+)\s*=>\s*_row\(\s*"(\w+)"\s*,\s*(true|false)\s*,\s*:(\w+)\s*,\s*"([^"]*)"\s*,\s*"([^"]*)"',
        src,
    ):
        rid, model, puls, shunt, label, color = m.groups()
        variant = ("pulsatile" if puls == "true" else "nonpulsatile") + _SHUNT_TAG[shunt]
        if model in tagged:
            variant += "_p{patient}"
        rows[rid] = Row(rid, model, variant, _TAG_RX.sub("{patient}", label), color)

    # The explicit named-tuple rows — the bistability basins, whose variant IS the basin,
    # and the patient rows (m1pt, m4pt, m7 …), whose variant is an expression.
    for m in re.finditer(
        r':(\w+)\s*=>\s*\(\s*model\s*=\s*"(\w+)"\s*,\s*variant\s*=\s*(.+?)\s*,\s*label\s*=\s*"([^"]*)"\s*,\s*color\s*=\s*"([^"]*)"',
        src, re.S,
    ):
        rid, model, variant, label, color = m.groups()
        rows[rid] = Row(rid, model, _variant_expr(variant),
                        _TAG_RX.sub("{patient}", label), color)

    if len(rows) < 20:
        raise SystemExit(f"deckboard: parsed only {len(rows)} registry rows from "
                         f"{REGISTRY_JL} — the row syntax moved, fix parse_registry().")
    return rows


def parse_deck_models() -> tuple[list[tuple[re.Pattern, list[str]]], list[str]]:
    """The `endswith(DECK, "_suffix") ? [ids] : …` chain in plotting.jl, in ORDER, plus the
    fallback at its end. Order matters and is preserved: `_bistability_m3_analysis` has to
    be tested before `_m3_analysis`, exactly as the Julia does.

    The per-patient branches are `occursin(r"_m5_vs_p\\d+$", DECK)` — an open set, one
    deck per recording — so both forms are read, each as a pattern, in the one order."""
    src = _uncommented(PLOTTING_JL)
    block = re.search(r"const MODELS = let over(.*?)\n\s*let ids", src, re.S)
    if not block:
        raise SystemExit("deckboard: could not find the MODELS chain in plotting.jl.")
    body = block.group(1)
    chain = [(re.compile(m.group(2)) if m.group(2) else re.compile(re.escape(m.group(1)) + "$"),
              re.findall(r":(\w+)", m.group(3)))
             for m in re.finditer(r'(?:endswith\(DECK,\s*"([^"]+)"\)|occursin\(r"([^"]+)",\s*DECK\))'
                                  r'\s*\?\s*\[([^\]]*)\]', body)]
    tail = re.findall(r":\s*\n\s*\[([^\]]*)\]\)?\s*:", body)
    fallback = re.findall(r":(\w+)", tail[-1]) if tail else ["m3p", "m3"]
    if len(chain) < 8:
        raise SystemExit(f"deckboard: parsed only {len(chain)} deck→models branches — "
                         "the chain in plotting.jl moved.")
    return chain, fallback


def parse_experiment_suffixes() -> re.Pattern:
    """The analysis suffixes registry.jl strips to get EXPERIMENT, as the SAME regex.
    Parsed from its alternation, so a suffix added there is honoured here without an edit
    — and kept a pattern, because some alternatives are (`m\\d+_vs_p\\d+`, one per
    recording). Python's alternation is leftmost-first like Julia's, which is what the
    order in registry.jl relies on."""
    src = _uncommented(REGISTRY_JL)
    m = re.search(r'r"_\(([^)]*)\)\$"\s*=>\s*""', src)
    if not m:
        raise SystemExit("deckboard: could not find the EXPERIMENT suffix regex.")
    return re.compile("_(" + m.group(1) + ")$")


def parse_experiment_for() -> dict[tuple[str, str], str]:
    """`EXPERIMENT_FOR` — (protocol, ramp_target) -> experiment — out of the models' own
    main.jl.

    THIS IS THE MAPPING THE BOARD WAS MISSING. A deck is `<experiment>_<suffix>`, and the
    experiment is not a label: main.jl DERIVES it from (PROTOCOL, RAMP_TARGET) and
    registry.jl derives the H5 path from it. So a command that names a deck but not its
    protocol does not draw that deck at the default protocol — it solves a DIFFERENT
    experiment and files it under a different folder, then draws the deck you asked for
    from whatever older exports happen to be there. `_DECK=jugular_compression_m1_vs_m4`
    with no `_PROTOCOL` writes `postural_changes/`, and nothing in the run says so.

    Read backwards (experiment -> the pair that produces it) this is also what lets the
    board FILL IN the protocol from the deck you clicked, which is the only reason a
    third and fourth control here do not become two more things to get wrong by hand.

    Every model's table is read and they must agree — they are copies of one table, and
    two copies that have drifted is precisely the thing worth failing on.
    """
    pairs: dict[tuple[str, str], str] = {}
    for path in MAIN_JL:
        if not os.path.isfile(path):
            continue
        block = re.search(r"EXPERIMENT_FOR\s*=\s*Dict\((.*?)\n\s*\)",
                          _uncommented(path), re.S)
        if not block:
            continue
        for m in re.finditer(r'\(\s*:(\w+)\s*,\s*:(\w+)\s*\)\s*=>\s*"(\w+)"',
                             block.group(1)):
            proto, ramp, experiment = m.groups()
            was = pairs.setdefault((proto, ramp), experiment)
            if was != experiment:
                raise SystemExit(
                    f"deckboard: the EXPERIMENT_FOR tables disagree — ({proto}, {ramp}) "
                    f"is \"{was}\" in one main.jl and \"{experiment}\" in {path}.")
    if not pairs:
        raise SystemExit(f"deckboard: could not parse EXPERIMENT_FOR from any of "
                         f"{', '.join(MAIN_JL)} — the table moved, fix "
                         f"parse_experiment_for().")
    return pairs


def parse_ramp_defaults() -> dict[str, tuple[float, str]]:
    """protocol -> (peak, unit) when `_RAMP_AMP` is NOT set, from Protocol.jl's two
    `something(RAMP_AMP, …)` defaults.

    Only ever shown, never sent: it is the placeholder that makes an empty AMP box mean
    something specific ("70 °") rather than "whatever the repo says today". So unlike the
    parsers above this one does not raise — a missed default costs a vaguer label, where a
    missed deck->runs mapping would cost a wrong picture.
    """
    out: dict[str, tuple[float, str]] = {}
    try:
        src = _uncommented(PROTOCOL_JL)
    except OSError:
        return out
    for proto, const, unit in (("angle", "Θ_PEAK_DEG", "°"),
                               ("pressure", "P_PEAK_MMHG", "mmHg"),
                               ("qprod", "Q_PEAK_ML_MIN", "mL/min")):
        m = re.search(const + r"\s*=\s*something\(RAMP_AMP,\s*([\d.eE\s*+/-]+?)\)", src)
        if m and re.fullmatch(r"[\d.eE\s*+/-]+", m.group(1)):
            try:
                out[proto] = (float(eval(m.group(1), {"__builtins__": {}}, {})), unit)
            except (ValueError, SyntaxError, ZeroDivisionError):
                pass
    return out


DEFAULT_PATIENT = parse_default_patient()
REGISTRY = parse_registry()
DECK_CHAIN, DECK_FALLBACK = parse_deck_models()
SUFFIX_RE = parse_experiment_suffixes()
EXPERIMENT_FOR = parse_experiment_for()
RAMP_DEFAULTS = parse_ramp_defaults()

# The two selectors, in the order the radio rows show them. PROTOCOLS is every protocol
# the table knows; `ramps_for` is which boundaries that protocol can drive — one for
# :angle and :static, two for :pressure — so a pair the models have no experiment for is
# not something the window can be put into.
PROTOCOLS = list(dict.fromkeys(p for p, _ in EXPERIMENT_FOR))


def ramps_for(protocol: str) -> list[str]:
    return [r for p, r in EXPERIMENT_FOR if p == protocol] or ["Theta"]


def pair_for_deck(deck: str) -> tuple[str, str] | None:
    """The (protocol, ramp_target) that produces this deck's experiment, read backwards
    out of EXPERIMENT_FOR. None for a folder whose stem is no experiment any model
    derives — a sweep, or a name that predates the table."""
    experiment = experiment_of(deck)
    return next((pair for pair, exp in EXPERIMENT_FOR.items() if exp == experiment), None)


def experiment_of(deck: str) -> str:
    return SUFFIX_RE.sub("", deck, count=1)


def models_of(deck: str) -> list[str]:
    for rx, ids in DECK_CHAIN:
        if rx.search(deck):
            return ids
    return DECK_FALLBACK


def is_patient_deck(deck: str) -> bool:
    """Does this deck overlay a per-patient run — an M5/M6/M7 row, or M1/M4 under a
    patient's stimulus? Those are the decks whose runs depend on WHICH recording."""
    return deck_kind(deck) == "analysis" and any(
        REGISTRY[r].per_patient for r in models_of(deck) if r in REGISTRY)


def patient_of(deck: str) -> str:
    """Which recording a patient deck is drawn for. The name says so for every
    per-recording suffix (`_m5_vs_p083`, `_m1_vs_m7_p137`). The rest (`_m5_vs_data`,
    `_m6_analysis`) were drawn under whatever _PATIENT the render ran with, and the deck's
    own deck.json records it in the variants of the runs it drew. Failing both,
    registry.jl's default."""
    m = re.search(r"_p(\d+)$", deck)
    if m:
        return m.group(1)
    try:
        with open(os.path.join(OUTPUT, deck, "deck.json"), encoding="utf-8") as f:
            runs = json.load(f).get("runs", [])
    except (OSError, ValueError):
        runs = []
    for r in runs:
        parts = str(r.get("path", "")).split("/")
        m = re.search(r"_p(\d+)", parts[3]) if len(parts) > 3 else None
        if m:
            return m.group(1)
    return DEFAULT_PATIENT


# --- What KIND of folder is this? --------------------------------------------
# Three kinds, and conflating them is how a status board starts lying:
#
#   "analysis"  <experiment>_<suffix>, drawn by plotting.jl over MODEL_REGISTRY ids.
#   "sweep"     <experiment>_<label>_sweep, drawn by plotting_sweep.jl. Its runs are ONE
#               store family per model — main_sweep.jl:442/453, SWEEP_DIR against
#               SWEEP_VARIANT = "sweep_<label>" — and no registry id names them, so
#               resolving it through MODEL_REGISTRY finds nothing and reports a folder full
#               of good figures as having no exports at all.
#   "unmapped"  everything else under modelplots/ — latex/, ursino_validation/, loose PNGs,
#               folders from a deck name this checkout no longer knows. `models_of` has a
#               FALLBACK ([:m3p, :m3]) because _DECK accepts anything, and applying it here
#               would invent two runs for a folder that never had them.
#
# Only the first two get a staleness verdict. An unmapped folder is listed and left alone:
# "I do not know what this is" is a true statement, and "NO EXPORT" is not.

def deck_kind(deck: str) -> str:
    if SUFFIX_RE.search(deck):
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
OUTPUT = os.path.join(REPO, "modelplots")


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
class Plotter:
    """One source file that DRAWS this deck, and how it stands against its pictures."""
    path: str                       # repo-relative: it is a path in this repo, not a file id
    mtime: datetime
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
    plotters: list[Plotter] = field(default_factory=list)

    @property
    def stale(self) -> list[Source]:
        return [s for s in self.sources if s.newer_than_plot]

    @property
    def missing(self) -> list[Source]:
        return [s for s in self.sources if s.path is None]

    @property
    def edited(self) -> list[Plotter]:
        """The drawing code that has moved since these PNGs were written, newest first."""
        return sorted((p for p in self.plotters if p.newer_than_plot),
                      key=lambda p: p.mtime, reverse=True)

    @property
    def badge(self) -> str:
        """WORST FIRST, and REPLOT last of the three: a deck whose exports are stale is
        also being drawn by newer code more often than not, and the badge should name the
        thing that costs minutes rather than the thing that costs seconds. REPLOT is
        therefore what is left when the runs behind the picture are fine and only the code
        that drew it moved on."""
        if self.kind == "unmapped":
            return "?"
        # BEFORE stale, because there is nothing to be stale AGAINST. Ranking it by the
        # runs would call a never-drawn deck `ok` the moment its exports happen to be
        # older than a folder holding no pictures at all.
        if not self.pngs:
            return "UNDRAWN"
        if self.stale:
            return "STALE"
        if self.missing:
            return "NO EXPORT"
        if self.edited:
            return "REPLOT"
        return "ok"


def decks(sims: list[Sim]) -> list[Deck]:
    """Every deck folder under modelplots/, with the runs it overlays resolved the way
    plot_runs.jl would resolve them right now.

    THE COMPARISON IS AGAINST THE OLDEST PNG, not the newest. A deck is re-rendered as a
    set, but a panel that failed to redraw keeps its old file — and it is that panel, the
    stalest one, that decides whether the FOLDER can be trusted.

    A FOLDER WITH NO PNGs IS LISTED, which it did not used to be, and that omission was
    the one this board could least afford. `jugular_compression_m1_vs_m4` is in Model_4's
    JOBS table, has a layout.jl, and had four successful solves filed under it — and it
    was invisible here, because the only thing it lacked was the pictures. So the deck you
    most need to draw was the one deck you could not click, and the board's answer to
    "why did nothing come out" was to not mention the deck at all.

    Not EVERY empty folder, though: a `modelplots/` subfolder with neither PNGs nor a
    layout.jl is not an undrawn deck, it is a directory. A layout.jl means the catalogue
    has been here and this is a deck; being in the JOBS table means the same thing.
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
        if not pngs and not (os.path.isfile(os.path.join(d, "layout.jl"))
                             or name in REPLOT_JOBS or is_patient_deck(name)):
            continue
        times = [_when(os.path.join(d, f)) for f in pngs]
        kind = deck_kind(name)
        # `drawn` is None with no PNGs, and every `newer_than_plot` test below already
        # guards on that — so an undrawn deck reports no stale sources rather than all of
        # them, which is right: nothing was drawn, so nothing drawn is out of date.
        deck = Deck(name, d, kind, pngs, min(times) if times else None,
                    max(times) if times else None)

        def add(rid: str, label: str, sim: Sim | None):
            if sim is None:
                deck.sources.append(Source(rid, label, None, None, None))
                return
            s = Source(rid, label, sim.dir, sim.stamp, sim.mtime)
            s.newer_than_plot = deck.drawn is not None and sim.mtime > deck.drawn
            deck.sources.append(s)

        if kind == "analysis":
            exp = experiment_of(name)
            patient = patient_of(name) if is_patient_deck(name) else ""
            for rid in models_of(name):
                row = REGISTRY.get(rid)
                if row is None:                  # an id the registry no longer carries
                    add(rid, "(not in registry)", None)
                else:
                    add(rid, row.label(patient),
                        by_family.get((row.model, exp, row.variant(patient))))
        elif kind == "sweep":
            exp, variant = sweep_family(name)
            # Every model that HAS this sweep family — the deck name does not say which
            # model ran it, so anything matching (experiment, variant) is a source.
            hits = [s for (m, e, v), s in by_family.items() if e == exp and v == variant]
            if not hits:
                add(variant or "sweep", f"{exp} / {variant or '?'}", None)
            for s in sorted(hits, key=lambda s: s.model):
                add(f"{s.model}:{variant}", f"{s.model} {variant}", s)

        # The other half of "is this picture still true": not the runs it came from, but
        # the code that drew it. Same comparison, same oldest-PNG rule.
        for f in plotter_files(name, kind):
            t = _when(f)
            deck.plotters.append(Plotter(os.path.relpath(f, REPO), t,
                                         deck.drawn is not None and t > deck.drawn))

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
    every file in one carries the same one. A stray .h5 under `modelplots/` is a deck folder
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

PLOT_ALL_JL = {n: os.path.join(REPO, MODEL_DIRS[n], "plot_all_decks.jl")
               for n in ("1", "3", "4")}


def _kwargs(s: str) -> dict[str, str]:
    return dict(re.findall(r'(\w+)\s*=\s*"([^"]*)"', s))


def parse_replot_jobs() -> dict[str, dict[str, dict]]:
    """deck folder -> {model: {kind, protocol, deck, sweep, ramp_target, beats_models}}.

    `kind` is which of the two builders the table used: a plain deck goes to
    replot_deck.jl, a sweep to main_sweep.jl with _REPLOT=1. The `deck` INSIDE a sweep
    entry is not the key — a sweep folder is drawn by the analysis deck it belongs to.

    KEYED BY MODEL, because there are several tables and M3's and M4's overlap.
    `postural_changes_k_sweep` is in both, each drawing it with its own analysis deck. And
    the model has to come from the TABLE rather than from the MODEL buttons:
    `postural_changes_m1_vs_m3` is an M1-and-M3 deck that M3's table redraws, and M1's
    driver would draw it with M1's `comps`. M1's own table holds only the decks M1 alone
    draws.
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

# PATIENT DECKS ARE IN NO JOBS TABLE. plot_all_decks.jl redraws M3's and M4's decks; the
# per-recording decks are an open set, one per prepared recording, and every one of them is
# drawn by M5's replot_deck.jl — the driver that loads the recording (Data.jl) before the
# plotter, which is what makes plotting.jl draw a deck as a PATIENT_DECK. M6's and M7's
# decks go through it too (fits/m1/Model_7/README.md). So a patient deck's job is built
# from its name rather than looked up.
PATIENT_DRIVER_MODEL = "5"


def patient_job(deck: str) -> dict | None:
    if not is_patient_deck(deck):
        return None
    protocol, ramp = pair_for_deck(deck) or ("pressure", "Pext")
    return dict(kind="deck", model=PATIENT_DRIVER_MODEL, deck=deck, protocol=protocol,
                ramp_target=ramp, patient=patient_of(deck))


def jobs_of(deck: str) -> dict[str, dict]:
    """Every replot job that claims this deck folder, by model."""
    jobs = REPLOT_JOBS.get(deck, {})
    if not jobs and (pj := patient_job(deck)):
        return {pj["model"]: pj}
    return jobs


def job_for(deck: str, model: str) -> dict | None:
    """This deck's replot job: the selected model's if that model draws it, otherwise the
    only model that claims it. Two claimants and neither selected is genuinely ambiguous —
    a sweep folder drawn by M3 and by M4 — so it returns None rather than picking."""
    jobs = jobs_of(deck)
    if model in jobs:
        return jobs[model]
    return next(iter(jobs.values())) if len(jobs) == 1 else None


def selection_for_deck(deck: str, model: str) -> tuple[str, str]:
    """The (protocol, ramp_target) this deck is MEANT to be run at, so the two selectors
    can fill themselves in from the deck rather than be remembered.

    EXPERIMENT_FOR read backwards is the authority — it is the table main.jl derives the
    experiment from, so a pair it gives back really does write the folder the deck name
    says. It only fails on a sweep, whose folder is `<experiment>_<label>_sweep` and so
    matches no experiment; the JOBS table names those explicitly, which is why it is the
    fallback and not the other way round.
    """
    pair = pair_for_deck(deck)
    if pair:
        return pair
    job = job_for(deck, model)
    if job:
        return job.get("protocol", "angle"), job.get("ramp_target", "Theta")
    return "angle", "Theta"


def replot_driver(job: dict) -> str | None:
    """The script that redraws without solving, if the job's model has one. A model
    without a replot_deck.jl gets None, and saying so is better than pinning a vintage onto
    an entry point that will cheerfully solve it again."""
    rel = f"{MODEL_DIRS[job['model']]}/" + ("main_sweep.jl" if job["kind"] == "sweep"
                                            else "replot_deck.jl")
    return rel if os.path.isfile(os.path.join(REPO, rel)) else None

# ══════════════════════════════════════════════════════════════════════════════
# THE PLOTTERS
# ══════════════════════════════════════════════════════════════════════════════
# WHICH FILES DRAW A DECK is a question with an answer already in the repo — the include
# graph — so it is walked rather than listed. A list of plotting sources in a Python file
# would miss the next `include` the day it is written, and miss it silently, which is the
# same failure mode the parsers above exist to avoid.
#
# THE WALK STARTS AT THE REPLOT DRIVER the JOBS table names for this deck, because that is
# literally the program that redraws it: every model's replot_deck.jl reaches into
# plotter/plotting.jl, and a hand-written mapping would have had each model's decks
# watching a plotting.jl in its own folder that does not exist.
#
# IT FOLLOWS ONLY THE DRIVER'S PLOTTING INCLUDE, not everything the driver includes. A
# driver also pulls in Model.jl, Params.jl and the rest of the solver, and the solver has
# NO SAY IN A REDRAW — a replot reads an .h5 and draws it. Editing Model.jl invalidates the
# export (which the store's own timestamps already show); it does not invalidate the
# picture drawn from that export. Past `plotting.jl` the closure is unrestricted, because
# everything it reaches — plot_runs.jl, panels_shared.jl, panel_catalogue.jl, registry.jl
# and its labels and colours — really does change what comes out.
#
# AND THE DECK'S OWN layout.jl, which is drawing code that lives in modelplots/<deck>/ and is
# the one plotter that differs per deck. It is safe to compare: panel_catalogue.jl rewrites
# it only when the content changes, so a redraw does not touch its mtime and cannot make a
# deck permanently accuse itself.

SWEEP_PLOTTING_JL = os.path.join(REPO, "plotter", "plotting_sweep.jl")

# The two include forms these sources use. Anything else — an include of a variable, a
# computed path — resolves to nothing and drops out of the closure: a plotter that cannot
# be named is better missing from the comparison than guessed at.
_INC_STR = re.compile(r'include\(\s*"([^"]+)"\s*\)')
_INC_JOIN = re.compile(r'include\(\s*joinpath\(([^)]*)\)\s*\)')
_IS_PLOTTER = re.compile(r'plotting(_sweep)?\.jl$')


def _include_targets(path: str) -> list[str]:
    """Absolute paths of the files `path` includes. Commented-out includes are not
    includes — hence _uncommented, the same strip the parsers above use."""
    text = _uncommented(path)
    here = os.path.dirname(path)
    out = [os.path.normpath(os.path.join(here, m.group(1))) for m in _INC_STR.finditer(text)]
    for m in _INC_JOIN.finditer(text):
        # joinpath(@__DIR__, "..", "plotter", "plotting.jl") — only the @__DIR__-anchored
        # form is resolvable from here, and it is the only one the sources use.
        toks = [here if t.group(0) == "@__DIR__" else t.group(1)
                for t in re.finditer(r'@__DIR__|"([^"]*)"', m.group(1))]
        if toks and toks[0] == here:
            out.append(os.path.normpath(os.path.join(*toks)))
    return out


_CLOSURE: dict[str, list[str]] = {}


def plotter_closure(entry: str) -> list[str]:
    """`entry` and everything it includes, transitively. Cached: plotting.jl is 3000 lines
    and every deck in the window asks for the same closure."""
    if entry in _CLOSURE:
        return _CLOSURE[entry]
    seen: list[str] = []
    queue = [entry]
    while queue:
        f = queue.pop(0)
        if f in seen or not os.path.isfile(f):
            continue
        seen.append(f)
        queue += _include_targets(f)
    _CLOSURE[entry] = seen
    return seen


def plotter_files(deck: str, kind: str) -> list[str]:
    """Every source that draws this deck folder, absolute paths, deduped.

    BOTH claimants when two models claim the deck. job_for() refuses to pick between them
    because it has to produce one command; this does not have to pick — a change in either
    model's drawing code is a reason to look at the folder again.

    The fallback, for a folder no JOBS table names, is plotter/plotting.jl — which is what
    the parsers above already treat as the authority, and is a great deal closer to the
    truth than watching nothing at all.
    """
    found: list[str] = []

    def add(f: str) -> None:
        if os.path.isfile(f) and f not in found:
            found.append(f)

    for job in jobs_of(deck).values():
        rel = replot_driver(job)
        if rel is None:                       # no replot driver, so nothing to walk
            continue
        driver = os.path.join(REPO, rel)
        add(driver)                           # it chooses the panels, so it is a plotter
        for inc in _include_targets(driver):
            if _IS_PLOTTER.search(inc):
                for f in plotter_closure(inc):
                    add(f)
    if not found:
        for entry in [PLOTTING_JL] + ([SWEEP_PLOTTING_JL] if kind == "sweep" else []):
            for f in plotter_closure(entry):
                add(f)
    add(os.path.join(OUTPUT, deck, "layout.jl"))
    return found


# ══════════════════════════════════════════════════════════════════════════════
# WHICH BRANCH
# ══════════════════════════════════════════════════════════════════════════════
# A run writes into simulations/ and modelplots/ and then UPLOADS the deck's PNGs to Google
# Drive. Neither the store, nor the deck folder, nor the upload records which checkout
# produced them — so a solve started from a side branch makes figures indistinguishable
# from the ones the shared code makes, and the only trace of the difference is a working
# tree you may well move off before anyone asks. The board cannot fix that. It can say it,
# once, in the last place where saying it is still free: directly above the run button.

def _git(*args: str) -> str:
    """git in the repo, or "". This window has to open with no git installed, on a copy
    with no .git in it, and mid-rebase — none of which is a reason to fail to open."""
    try:
        r = subprocess.run(["git", "-C", REPO, *args], capture_output=True, text=True,
                           timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def main_branch() -> str:
    """What "main" is called here. origin/HEAD first — it is the remote's own statement of
    its default branch — then whichever conventional name actually exists locally."""
    head = _git("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if head.startswith("origin/"):
        return head.split("/", 1)[1]
    for name in ("main", "master"):
        if _git("rev-parse", "--verify", "--quiet", f"refs/heads/{name}"):
            return name
    return "main"


def branch_state() -> tuple[str, str, bool]:
    """(branch, main, on_main).

    A DETACHED HEAD counts as off-main and says so by name, because it is the worse case of
    the two: a branch at least still exists to go back to.
    """
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    if not branch:
        return "", "", True            # no git to ask: nothing to warn about
    if branch == "HEAD":
        branch = "detached HEAD at " + (_git("rev-parse", "--short", "HEAD") or "?")
        return branch, main_branch(), False
    m = main_branch()
    return branch, m, branch == m


def branch_warning() -> str:
    """The line itself, or "" when there is nothing to say."""
    branch, m, on_main = branch_state()
    if on_main or not branch:
        return ""
    return (f"⚠ ON {branch} — {m} is the main branch here. What this run solves and "
            f"uploads is drawn by code that is not on {m}.")


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
                  plot: bool, pin: str = "", protocol: str = "angle",
                  ramp: str = "Theta", amp: str = "") -> tuple[list[str], dict[str, str], str]:
    """(argv, env-overlay, the copy-pasteable line). One place, so what the window SHOWS,
    what it RUNS and what COPY gives you cannot drift — all three render from this dict.

    THE PROTOCOL IS ALWAYS WRITTEN OUT, on both paths and even when it is the default.
    This used to be left off the solving command entirely, and leaving it off is not
    "unspecified" — it is `_PROTOCOL=angle`, main.jl's default, which derives the
    experiment `postural_changes` and writes there. So a `jugular_compression_*` deck ran
    from this window solved a TILT, filed it under postural_changes/, and then drew the
    jugular deck from whatever stale pressure exports were already on disk. Nothing
    errored, because nothing was wrong with the run — only with which run it was. Named
    explicitly, the line is also the one in the notes:

        _DECK=jugular_compression_m1_vs_m4 _RUN_IDS=m4,m4p \
            _PROTOCOL=pressure _RAMP_TARGET=Pext julia --project=. Model_3/main.jl

    THE MODEL IS THE FOLDER. The command runs that model's own entry point, named in
    MODEL_DIRS / SOLVE_ENTRY, from the repo root — there is no root dispatcher to send a
    `_MODEL` to. A PATIENT deck also carries `_PATIENT`, on both paths: the recording
    decides the protocol's timing, the fitted parameters and the store variant, and a run
    without it is patient 081's.

    `amp` is `_RAMP_AMP` — HOW FAR the ramp goes, degrees or mmHg by protocol (Protocol.jl
    owns both, and the units). Empty means "do not set it", which is not the same as
    sending its default: an unset knob follows Protocol.jl if that file changes, and it
    keeps the command short for the case that is still 90% of runs.

    WITH A PIN it is a DIFFERENT command, not the same one with a variable bolted on. A
    pinned vintage means "draw what is already on disk", and that is replot_deck.jl — or
    main_sweep.jl with _REPLOT=1 for a sweep — plus the deck's protocol. main.jl would
    accept _SIM_STAMP and then solve anyway, which is the one outcome a pin is chosen to
    avoid. When the model has no replot driver, or the deck is not in the table, it falls
    back to main.jl carrying the pin and the window says plainly that this will still
    solve.

    AND SO IS AN EMPTY `run_ids`, for the same reason and by the same route — see the
    comment on `draw_only` below. A pin says WHICH vintage to draw; no ids ticked says
    only THAT this is a draw, and the driver then resolves each id to its newest vintage
    on its own. Both fall back to main.jl the same way when the deck has no driver.
    """
    # NO IDS TICKED IS AN INSTRUCTION, not an omission. main.jl with no _RUN_IDS does
    # the opposite of nothing: it solves EVERY id in that model's registry, minutes of
    # solving nobody asked for, written over the exports the deck is drawn from. What an
    # empty tick list actually means here is "solve nothing, draw the deck" — so it goes
    # to the same replot driver a pin does, just without a _SIM_STAMP: newest vintage of
    # each id, drawn, solved never. Untick with the draw box OFF and it is left alone,
    # because "solve everything, draw nothing" is the only reading left once drawing is
    # off, and it is a real thing to ask for.
    draw_only = bool(pin) or (plot and not run_ids)
    job = job_for(deck, model) if draw_only else None
    driver = replot_driver(job) if job else None
    if draw_only and job and driver:
        # The protocol here is the SELECTED one, not the JOBS table's, because selecting
        # the vintage set it from that vintage's own meta.json — what was actually solved
        # beats what the table says should redraw it, and they only differ when one of
        # them is wrong.
        env = {"_SIM_STAMP": pin} if pin else {}
        env.update({"_PROTOCOL": protocol, "_RAMP_TARGET": ramp,
                    # A sweep folder is drawn by the analysis deck it belongs to, so the
                    # _DECK here is the table's, not the folder you clicked.
                    "_DECK": job.get("deck", deck)})
        if amp.strip():
            # Does not change a redraw — a replot solves nothing — but it keeps `params`
            # describing the vintage being drawn rather than the default, and selecting
            # an export fills this in from that export's own meta.json.
            env["_RAMP_AMP"] = amp.strip()
        if job["kind"] == "sweep":
            env.update(_SWEEP=job.get("sweep", ""), _REPLOT="1", _SIM_MODE="1")
        elif job.get("beats_models"):
            env["_BEATS_MODELS"] = job["beats_models"]
        if job.get("patient"):
            env["_PATIENT"] = job["patient"]
        argv = ["julia", "--project=.", driver]
        return _finish(env, argv, extra)
    entry = f"{MODEL_DIRS[model]}/{SOLVE_ENTRY[model]}"
    argv = ["julia", *SOLVE_FLAGS.get(model, []), "--project=.", entry]
    if model in SOLVE_ENV:
        # M7's export stage takes the patient and nothing else — no deck, no ids, no
        # protocol: it solves its four runs on that recording's own compression.
        env = dict(SOLVE_ENV[model], _PATIENT=patient_of(deck))
        return _finish(env, argv, extra)
    env = {"_DECK": deck}
    if pin:
        env["_SIM_STAMP"] = pin
    if run_ids:
        env["_RUN_IDS"] = ",".join(run_ids)
    if is_patient_deck(deck):
        env["_PATIENT"] = patient_of(deck)
    env["_PROTOCOL"] = protocol
    env["_RAMP_TARGET"] = ramp
    if amp.strip():
        env["_RAMP_AMP"] = amp.strip()
    if not plot:
        env["_NOPLOT"] = "1"
    # Free-form `K=V K=V`, applied last so it can override anything above.
    return _finish(env, argv, extra)


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

# This repo carries its own copy, so the banner mark does not depend on the checkout it reads;
# the checkout's copy is the fallback for an older clone of this repo.
BRAIN_SVG = next((p for p in (os.path.join(os.path.dirname(os.path.abspath(__file__)), "brain.svg"),
                              os.path.join(REPO, "brain.svg")) if os.path.isfile(p)),
                 os.path.join(REPO, "brain.svg"))


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

    try:
        import tkinter as tk
    except ModuleNotFoundError as e:
        # The window needs Tk; --report above does not, which is why the import lives
        # here and not at the top. A bare ModuleNotFoundError names `tkinter` and
        # nothing else — not which interpreter lacks it, and not that the fix is a
        # SYSTEM package rather than a pip install, which is the part that makes this
        # look like a broken script rather than a missing dependency.
        raise SystemExit(
            f"deckboard: {sys.executable} has no tkinter, so the window cannot open.\n"
            f"           ({e})\n"
            "  install it:  sudo apt install python3-tk\n"
            "  or run under an interpreter that already has it, e.g.\n"
            "      ~/anaconda3/bin/python3 deckboard/deckboard.py\n"
            "  --report needs no Tk and works on any interpreter.") from None

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
        # An explicit "load this deck" is exactly when the protocol should come back from
        # the deck rather than from whatever was selected for the last one.
        state["pair_touched"] = False
        sync_pair()
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
            # WITHOUT `command` these were the only controls in the window that did not
            # redraw the line below them: ticking or unticking an id changed what ARM
            # would run — `spawn` rebuilds from `current()` — while the command on screen
            # still showed the previous set. Untick everything and _RUN_IDS stayed
            # written up there, which is the one case where the displayed command and the
            # one that runs mean OPPOSITE things: a draw shown as a solve.
            tk.Checkbutton(id_frame, text=rid, variable=v, command=lambda: preview(),
                           bg=PANEL, fg=INK,
                           selectcolor=BG, activebackground=PANEL, activeforeground=ACCENT,
                           font=MONO, bd=0, highlightthickness=0).pack(side="left")
        preview()

    for tag in MODEL_DIRS:
        tk.Radiobutton(mrow, text=f" M{tag} ", value=tag, variable=model_var,
                       command=rebuild_ids, bg=PANEL, fg=ACCENT2, selectcolor=EDGE,
                       activebackground=EDGE, activeforeground=BG, font=MONO_B,
                       indicatoron=False, bd=0, width=5).pack(side="left", padx=2)

    tk.Label(runbox, text="RUN IDS   (none ticked = redraw the deck, solve nothing)",
             bg=PANEL, fg=DIM, font=MONO, anchor="w").pack(fill="x", padx=8, pady=(6, 0))
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

    # --- protocol, ramp target, ramp amplitude ------------------------------
    # The three things that decide WHAT IS SOLVED, and until now the three things this
    # window could not say. They sit directly under DECK because they are not independent
    # of it: the deck names an experiment, and EXPERIMENT_FOR says which pair produces
    # that experiment — so choosing a deck FILLS THESE IN, and the only reason to touch
    # them by hand is to run a deck's protocol at something other than its usual setting.
    #
    # Once touched they stay put (state["pair_touched"]), because a selector that silently
    # reset itself when you edited the deck name would be worse than no selector. Clicking
    # a deck in PLOTS, or an export in EXPORTS, is an explicit "load this" and does reset
    # it — those are the gestures that mean "give me the right settings for this thing".
    proto_var = tk.StringVar(value="angle")
    ramp_var = tk.StringVar(value="Theta")

    prow = tk.Frame(runbox, bg=PANEL)
    prow.pack(fill="x", padx=8, pady=(4, 0))
    tk.Label(prow, text="PROTOCOL", bg=PANEL, fg=DIM, font=MONO, width=8,
             anchor="w").pack(side="left")

    rrow = tk.Frame(runbox, bg=PANEL)
    rrow.pack(fill="x", padx=8, pady=(2, 0))
    tk.Label(rrow, text="RAMP", bg=PANEL, fg=DIM, font=MONO, width=8,
             anchor="w").pack(side="left")
    ramp_holder = tk.Frame(rrow, bg=PANEL)
    ramp_holder.pack(side="left")

    def touch_pair():
        state["pair_touched"] = True
        rebuild_ramps()

    def rebuild_ramps(*_):
        """The ramp targets THIS protocol has an experiment for — both boundaries for
        :pressure, Theta alone for :angle and :static. A pair outside the table has no
        experiment to be filed under, so it is not offered rather than warned about."""
        for w in ramp_holder.winfo_children():
            w.destroy()
        opts = ramps_for(proto_var.get())
        if ramp_var.get() not in opts:
            ramp_var.set(opts[0])
        for r in opts:
            tk.Radiobutton(ramp_holder, text=f" {r} ", value=r, variable=ramp_var,
                           command=lambda: (state.update(pair_touched=True), preview()),
                           bg=PANEL, fg=ACCENT2, selectcolor=EDGE, activebackground=EDGE,
                           activeforeground=BG, font=MONO_B, indicatoron=False, bd=0,
                           width=7).pack(side="left", padx=2)
        preview()

    for pname in PROTOCOLS:
        tk.Radiobutton(prow, text=f" {pname} ", value=pname, variable=proto_var,
                       command=touch_pair, bg=PANEL, fg=ACCENT2, selectcolor=EDGE,
                       activebackground=EDGE, activeforeground=BG, font=MONO_B,
                       indicatoron=False, bd=0, width=9).pack(side="left", padx=2)

    amp_entry = labelled_entry("AMP", "")
    amp_lbl = tk.Label(runbox, text="", bg=PANEL, fg=DIM,
                       font=("DejaVu Sans Mono", 8), anchor="w")
    amp_lbl.pack(fill="x", padx=8)

    def sync_pair(force: bool = False):
        """Put the selectors where the DECK says they belong, unless they were set by
        hand. Called on every edit of the deck name, so a deck typed in is as correct as
        one clicked."""
        if state["pair_touched"] and not force:
            return
        proto, ramp = selection_for_deck(deck_entry.get().strip(), model_var.get())
        proto_var.set(proto)
        ramp_var.set(ramp)
        rebuild_ramps()

    deck_entry.bind("<KeyRelease>", lambda _e: sync_pair())

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

    # THE COMMAND, AS TEXT A CURSOR CAN BE DRAGGED THROUGH. It was a Label, and a Label
    # holds no selection — so COPY was the only way this line got out of the window, and
    # Tk's clipboard belongs to THIS process: it empties when deckboard closes, which is
    # exactly when you go to paste it into a terminal. A Text keeps the selection, answers
    # Ctrl+C, and on X11 puts it in PRIMARY as well, so middle-click works too.
    #
    # It stays in `normal` state and refuses edits through its bindings instead of being
    # `disabled`, because a disabled Text takes no keyboard focus on some builds and
    # Ctrl+C from the keyboard is half the point.
    cmd_txt = tk.Text(runbox, bg=BG, fg=ACCENT2, font=MONO, bd=0, height=3, wrap="word",
                      highlightbackground=EDGE, highlightthickness=1, padx=4, pady=3,
                      selectbackground=EDGE, selectforeground=INK, cursor="xterm",
                      insertbackground=ACCENT)
    cmd_txt.pack(fill="x", padx=8, pady=6)

    _NAV = {"Left", "Right", "Up", "Down", "Home", "End", "Prior", "Next",
            "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R"}

    def _readonly(event):
        """Let through what reads, swallow what writes. Ctrl-modified c/a/x and the
        motion keys move a selection about; everything else would edit a line whose
        whole value is that it is the line that will run."""
        if event.state & 4 and event.keysym.lower() in ("c", "a", "insert"):
            return None
        return None if event.keysym in _NAV else "break"

    cmd_txt.bind("<Key>", _readonly)
    cmd_txt.bind("<<Cut>>", lambda _e: "break")
    cmd_txt.bind("<<Paste>>", lambda _e: "break")
    cmd_txt.bind("<Button-2>", lambda _e: "break")          # X11 middle-click paste
    cmd_txt.bind("<Control-a>",
                 lambda _e: (cmd_txt.tag_add("sel", "1.0", "end-1c"), "break")[1])

    def set_cmd(text: str) -> None:
        """Grow to fit rather than scroll — a command you have to scroll to read is one
        you cannot check before arming it."""
        cmd_txt.delete("1.0", "end")
        cmd_txt.insert("1.0", text)
        cmd_txt.update_idletasks()      # -displaylines needs the wrap already applied
        try:
            n = cmd_txt.count("1.0", "end", "displaylines")
            n = int(n[0] if isinstance(n, (list, tuple)) else n)
        except (TypeError, ValueError, tk.TclError):
            n = 3
        cmd_txt.configure(height=max(2, min(n, 8)))

    warn_lbl = tk.Label(runbox, text="", bg=PANEL, fg=WARN, font=MONO, anchor="w",
                        justify="left", wraplength=430)
    warn_lbl.pack(fill="x", padx=8)

    # The launcher's state. No process lives here: a run is handed to a terminal and
    # forgotten about. "pin" is the vintage clicked in EXPORTS below, and it changes WHICH
    # command gets built rather than just adding a variable to the one that was there.
    state = {"argv": None, "env": None, "armed": False, "pin": "", "sel": None,
             "seltag": "", "del_armed": False, "pair_touched": False}

    def current():
        ids = [k for k, v in id_vars.items() if v.get()]
        return build_command(model_var.get(), deck_entry.get().strip(), ids,
                             extra_entry.get(), plot_var.get(), state["pin"],
                             proto_var.get(), ramp_var.get(), amp_entry.get())

    def preview(*_):
        argv, env, shown = current()
        state.update(argv=argv, env=env, armed=False)
        set_cmd(shown)
        run_btn.configure(text="▶ ARM", bg=EDGE)
        notes = []
        deck = deck_entry.get().strip()
        want = models_of(deck)
        proto, ramp = proto_var.get(), ramp_var.get()
        experiment = EXPERIMENT_FOR.get((proto, ramp), "?")

        # What an empty AMP box means, in the unit THIS protocol is read in — and for
        # :static, that there is no ramp for it to be the amplitude of.
        if proto == "static":
            amp_lbl.configure(text="         the static protocol has no ramp — AMP and "
                                   "RAMP do not reach the solve")
        else:
            peak, unit = RAMP_DEFAULTS.get(proto, (None, ""))
            amp_lbl.configure(
                text=f"         _RAMP_AMP in {unit or 'the protocol unit'} · blank = "
                     f"{'%g %s' % (peak, unit) if peak is not None else 'the protocol default'}"
                     f" · writes simulations/m{model_var.get()}/{experiment}/")

        # THE ONE THAT MATTERS. The experiment is derived from (protocol, ramp), the H5
        # path is derived from the experiment, and the deck is drawn from whatever is at
        # that path — so a pair that disagrees with the deck name does not fail, it solves
        # the other experiment and then draws the deck from exports nobody just made.
        implied = pair_for_deck(deck)
        if implied and implied != (proto, ramp) and not state["pin"]:
            notes.append(f"{proto}/{ramp} solves into {experiment}/, but {deck} is drawn "
                         f"from {experiment_of(deck)}/ — the deck will be redrawn from "
                         f"OLDER exports. Was this meant to be "
                         f"{implied[0]}/{implied[1]}?")
        chosen = [k for k, v in id_vars.items() if v.get()]
        # The deck draws a fixed id set; anything you solve outside it is stored but not
        # drawn, and anything in it you did NOT solve is drawn from an older vintage. A
        # replot has no say in what it solves, so the note is about solving only.
        if chosen and not state["pin"] and not set(chosen) & set(want):
            notes.append(f"none of {', '.join(chosen)} is drawn by {deck} "
                         f"(it overlays {', '.join(want)})")
        # THE SAME TRAP, reached the other way. No ids ticked is read as "draw only", but
        # that reading needs a replot driver to carry it — and without one it falls back to
        # main.jl with no _RUN_IDS, which is not a small solve, it is EVERY id this model
        # has. Said here because the command line itself looks innocuous: it is the
        # variable that ISN'T in it that costs the afternoon.
        if plot_var.get() and not chosen and not state["pin"]:
            job = job_for(deck, model_var.get())
            if job is None or replot_driver(job) is None:
                notes.append(f"no ids ticked and no replot driver for {deck} — this "
                             f"cannot draw-only, it will SOLVE every M{model_var.get()} "
                             f"run id ({', '.join(sorted(id_vars))})")
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
        if model_var.get() in SOLVE_ENV and (chosen or not plot_var.get()) and not state["pin"]:
            notes.append(f"M{model_var.get()} solves through "
                         f"{SOLVE_ENTRY[model_var.get()]}, which exports all of its runs for "
                         f"patient {patient_of(deck)} and draws nothing — untick the ids to "
                         f"redraw {deck} afterwards")
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
        if x.model.startswith("m") and x.model[1:] in MODEL_DIRS:
            model_var.set(x.model[1:])
            rebuild_ids()
        if x.meta.get("deck"):
            deck_entry.delete(0, "end")
            deck_entry.insert(0, x.meta["deck"])
        # The vintage RECORDS what it was solved at — protocol, boundary and, since the
        # ramp became a knob, its amplitude. That beats deriving them from the deck name:
        # it is the difference between "what this deck is usually run at" and "what this
        # file on disk actually is", and a vintage solved at a non-default amplitude is
        # otherwise indistinguishable from one that was not.
        state["pair_touched"] = False
        if x.meta.get("protocol"):
            proto_var.set(x.meta["protocol"])
            ramp_var.set(x.meta.get("ramp_target", "Theta"))
            rebuild_ramps()
        else:
            sync_pair()
        amp_entry.delete(0, "end")
        if x.meta.get("ramp_amp") is not None:
            amp_entry.insert(0, f"{float(x.meta['ramp_amp']):g}")
        # A modelplots/ .h5 predates the store and has no vintage to pin. It is listed so it
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
                                         f"amp {m['ramp_amp']:g}"
                                         if isinstance(m.get("ramp_amp"), (int, float))
                                         else "",
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
        """Onto the SYSTEM clipboard wherever there is one. Tk's own clipboard is owned by
        this process and is emptied the moment the window closes — so the old copy-then-
        close-then-paste got you nothing, silently, which is a worse outcome than no COPY
        button at all. wl-copy and xclip hand it to something that outlives us; Tk stays as
        the fallback, and the label says WHICH it got so an empty paste is not a mystery."""
        line = current()[2]
        for tool in (["wl-copy"], ["xclip", "-selection", "clipboard"]):
            exe = shutil.which(tool[0])
            if exe is None:
                continue
            try:
                subprocess.run([exe, *tool[1:]], input=line.encode(), check=True,
                               timeout=5)
            except (OSError, subprocess.SubprocessError):
                continue
            launch_lbl.configure(text=f"(command copied — {tool[0]})", fg=ACCENT2)
            return
        root.clipboard_clear()
        root.clipboard_append(line)
        root.clipboard_get()            # own the selection before anyone asks for it
        launch_lbl.configure(text="(copied to Tk's own clipboard — it empties when this "
                                  "window closes; select the line above instead)", fg=WARN)

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
    sync_pair()
    if selftest:
        root.update_idletasks()
        root.update()
        argv, env, shown = current()
        print("deckboard --selftest: widget tree built and rendered")
        print(f"  {len(sims)} vintages · {len(ds)} decks · {nstale} stale")
        print(f"  default command (nothing ticked, so a draw): {shown}")
        assert argv[:1] == ["julia"] and "_DECK" in env, "command builder is wrong"
        # The protocol is the whole point of the pair of selectors: a command that names a
        # deck must also name the protocol that writes that deck's experiment, or it
        # solves a different one. Checked against EXPERIMENT_FOR rather than a literal, so
        # this follows the models' own table.
        assert EXPERIMENT_FOR.get((env["_PROTOCOL"], env["_RAMP_TARGET"])) == \
            experiment_of(env["_DECK"]), \
            f"{env['_DECK']} would be solved as {env['_PROTOCOL']}/{env['_RAMP_TARGET']}"
        # And the ramp knob reaches it, in one number.
        amp_entry.insert(0, "20")
        assert current()[1].get("_RAMP_AMP") == "20", "_RAMP_AMP did not reach the env"
        amp_entry.delete(0, "end")
        assert "_RAMP_AMP" not in current()[1], "an empty AMP box must not set the knob"
        print(f"  protocols: {PROTOCOLS} · ramp defaults: {RAMP_DEFAULTS}")
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
            # Clearing the pin does NOT go back to solving on its own — `select` unticks
            # every id, and no ids ticked is still a draw. Ticking one is what makes it a
            # solve, and that round trip is the whole of the new rule.
            assert not current()[0][-1].endswith("/main.jl"), \
                "with no ids ticked the board must draw, not solve"
            rid = sorted(id_vars)[0]
            id_vars[rid].set(True)
            argv, env, shown = current()
            assert argv[-1].endswith("/main.jl") and env.get("_RUN_IDS") == rid, \
                f"ticking {rid} did not restore the solving entry point: {shown}"
            print(f"  ticked {rid}:")
            print(f"    {shown}")
            id_vars[rid].set(False)
            assert "_RUN_IDS" not in current()[1], "unticking left _RUN_IDS in the command"
        # Every model button has somewhere to go, and a patient deck draws through M5's
        # driver with its recording named.
        for m in MODEL_DIRS:
            assert os.path.isfile(os.path.join(REPO, MODEL_DIRS[m], SOLVE_ENTRY[m])), \
                f"M{m} has no {MODEL_DIRS[m]}/{SOLVE_ENTRY[m]}"
        pdeck = next((d for d in ds if is_patient_deck(d.name)), None)
        if pdeck is not None:
            argv, env, shown = build_command("5", pdeck.name, [], "", True, "",
                                             *selection_for_deck(pdeck.name, "5"))
            assert argv[-1].endswith("replot_deck.jl") and env.get("_PATIENT") == \
                patient_of(pdeck.name) and env.get("_PROTOCOL") == "pressure", \
                f"patient deck {pdeck.name} built {shown}"
            print(f"  patient deck {pdeck.name} (p{patient_of(pdeck.name)}):")
            print(f"    {shown}")
        print(f"  terminals available: "
              f"{[n for n, _ in TERMINALS if shutil.which(n)] or 'NONE — set $TERMINAL'}")
        root.destroy()
        return 0
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
