# Deckboard

What has been solved, what has been drawn, and which is stale — for the brainphysio
human family (M1–M7).

One window over two trees that nothing else joins: the simulation store
(`simulations/<model>/<experiment>/<variant>/<stamp>/run.h5`) and the deck folders
(`modelplots/<deck>/*.png`). For every deck it resolves the runs that deck overlays,
compares their exports and the code that draws them against the PNGs, and gives you
the command that brings the deck up to date — a replot when only the drawing code
moved, a solve when the runs did. The command runs in a new terminal; nothing is
launched from the window itself.

The header of `deckboard.py` explains every design decision at length.

## Getting it

Deckboard is a git submodule of the brainphysio repo, at `deckboard/`, on the
`human_m123` branch:

```bash
git clone --recursive <brainphysio repo>          # a fresh clone
git submodule update --init deckboard             # an existing clone, on human_m123
```

A plain clone does not fetch it, which is the point: the handover repo does not
carry it.

## Running it

From the root of the brainphysio checkout:

```bash
python3 deckboard/deckboard.py              # the window
python3 deckboard/deckboard.py --report     # the same data as text, no window
python3 deckboard/deckboard.py --selftest   # build the window, render one frame, exit
python3 deckboard/deckboard.py --repo PATH  # read another checkout (a worktree, a clone)
```

The window needs tkinter, a separate package on Debian/Ubuntu:
`sudo apt install python3-tk`. `--report` needs nothing but the standard library.

## What it reads

Everything it knows about the models is parsed from the checkout at startup, so a
new registry row, deck or suffix shows up without an edit here:

| fact | from |
|---|---|
| run ids, their store variant, label, colour | `plotter/registry.jl` (`MODEL_REGISTRY`, `PATIENT_TAG`) |
| experiment suffixes | `plotter/registry.jl` (the `EXPERIMENT` regex) |
| deck → run ids | `plotter/plotting.jl` (the `MODELS` chain) |
| (protocol, ramp) → experiment | each model's `main.jl` (`EXPERIMENT_FOR`) |
| replot jobs for M3/M4 decks and sweeps | `Model_3_celia/`, `Model_3/` `plot_all_decks.jl` |
| the store | `simstore.py` |

The one thing stated rather than parsed is `MODEL_DIRS` in `deckboard.py`: which
folder a registry model id lives in (`m3` → `Model_3_celia`, `m4` → `Model_3`,
`m5` → `fits/m3/Model_5`, …). If one of those folders is missing, the board refuses to
open rather than guess.

Patient decks (`_m5_vs_p083`, `_m1_vs_m7_p137`, `_m6_vs_data`, …) are drawn through
`fits/m3/Model_5/replot_deck.jl` with the recording's `_PATIENT`, taken from the deck
name or, failing that, from the deck's own `deck.json`.

## Changing it

Commit here, then move the pointer in brainphysio so a recursive clone gets the new
version:

```bash
cd deckboard && git commit -am "…" && git push
cd .. && git add deckboard && git commit -m "deckboard: …"
```

## The dock launcher

`launcher/deckboard.sh` opens the window and turns a failure into a desktop
notification. To install it for the checkout at `~/brainphysio_celia`:

```bash
sed "s|^REPO=.*|REPO=\${DECKBOARD_REPO:-$HOME/brainphysio_celia}|" \
    deckboard/launcher/deckboard.sh > ~/.local/bin/deckboard.sh
chmod +x ~/.local/bin/deckboard.sh
sed "s|%HOME%|$HOME|g" deckboard/launcher/deckboard.desktop \
    > ~/.local/share/applications/deckboard.desktop
cp brain.svg ~/.local/share/icons/brain.svg
```
