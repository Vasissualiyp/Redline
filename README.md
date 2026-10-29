# Redline

Review AI-suggested edits to an Overleaf paper with vim keys.

An agent proposes edits through an MCP server. Redline writes them into the
`.tex` as LaTeX `changes` markup (`\added`, `\deleted`, `\replaced`, `\chcomment`)
that is guaranteed to compile. You then walk the suggestions in nvim and accept,
reject or annotate them one by one or in bulk, with the compiled PDF following
your cursor in a kitty-graphics pane.

```
┌ nvim: paper.tex (review mode) ───────────┐┌ agent (claude / opencode) ──┐
│ The sink accretes ← grows at a rate …     ││ > tighten §3.2              │
│ ‡ Shorter wording.                 C12    ││ created C12 1f8b55 replaced │
│ ]c next  a accept  x reject  n %N note    │├ PDF (kitty graphics) ───────┤
│ d ask the agent  A accept section  c make ││  page 8, change highlighted │
└───────────────────────────────────────────┘└─────────────────────────────┘
 window 2 "code": nvim in the analysis repo | shell
```

## Quick start

```bash
nix develop ~/Software/Redline#paper           # redline + ols + poppler/tmux/git, clean PYTHONPATH
cd ~/Software/Overleaf-nix-flake/Pop\ III\ m12f
redline init --code-dir ~/research/trillium/scratch/SHIVAN/analysis
redline open                                  # tmux session redline-<paper>
```

The `paper` shell expects TeX (latexmk and synctex), nvim, kitty and zotero-mcp
to come from the system. `ols` is built from `overleaf-sync.nix` in the
Overleaf-sync-nix-flake repo, which this flake takes as a single-file input.
After you change that file, run `nix flake update overleaf-sync`.
`packages.redline` can instead go into home-manager or `nix profile install`.

`redline init` does three things:
- writes `.redline.toml`;
- adds redline's files to `.olignore` and `.gitignore`, so nothing of it reaches Overleaf;
- registers the `redline` and `zotero` MCP servers for the agent (`.mcp.json`, or
  `opencode.json` with `agent_kind = "opencode"`).

It also downloads the current Overleaf state as the merge base, which you can skip with `--no-sync`.
Agents load the `redline` skill from `~/Software/ai-skills`, which also covers refcheck, zotero and the others.

Without installing, you can run it from the checkout:
`nix develop ~/Software/Redline -c python -m redline …`.

## Review mode (nvim)

`<leader>R` toggles it. While review mode is on, single keys are rebound and
gitsigns' `]c` is restored when you leave.

| key | action |
|---|---|
| `]c` `[c` / `]n` `[n` | next/prev change / next/prev unrated change |
| `a` / `x` | accept / reject, then jump to the next change |
| `n` | keep pending: `%N <note>` (the note is feedback for the agent) |
| `y` / `X` | mark `%Y` / `%X` for later bulk application |
| `e` / `C` | edit the suggested text / its comment in place |
| `d` (also visual) | ask the agent about this change / the selection |
| `A` `R` `M` (also visual) | accept / reject / apply markers in the section, heading or selection |
| `K` / `gl` | details / list of all changes (Telescope) |
| `p` / `f` | show in the PDF pane / toggle follow |
| `c` / `F` | compile (errors go to quickfix) / send the errors to the agent |
| `P` / `L` | push to / pull from Overleaf (3-way merge) |
| `q` `<Esc>` `i` | leave review mode |

Outside review mode:
- `]r` / `[r` jump between changes.
- `<leader>ja{motion}` and `<leader>jx{motion}` accept or reject a range, e.g.
  `<leader>jaip`. `<leader>jA` / `<leader>jX` act on the change under the cursor.
- `<leader>jd` asks the agent, `<leader>jc` compiles and `<leader>jr` toggles the raw markup.
- `:Redline {review,list,pending,notes,compile,fix,lint [fix],push,pull,acceptall}`.

To put the status in your statusline, use `require('redline').status()`
(e.g. `REVIEW C12/49 · §3.2 · 2 marked`).

Undo is ordinary vim undo. Accepting and rejecting are applied as buffer edits.

## Markers (when reviewing in Overleaf instead)

Put these at the end of a line in Overleaf, then tell the agent "apply my markers":

- `%Y`: accept.
- `%N why?`: keep pending. The note goes to the agent.
- `%X`: reject.

From the shell, `redline markers` does the same. Bulk accepts always honour the markers.

## CLI

```
redline list [-s SECTION] [--until SECTION] [-m N] [-c TEXT] [ids...]
redline accept --until "Simulation and Methods" -x a3f9c2
redline reject C12 C13
redline lint --fix            # mbox citations, split paragraphs, move float comments, label-first captions
redline compile [--isolate]   # latexmk into .redline/build, errors mapped to changes
redline sync init|pull|push   # Overleaf via ols, 3-way merged; push = pull+lint+compile+ols -l
redline undo                  # revert the last redline write (keeps later unrelated edits)
```

## How it fits together

- `redline/parse.py`: the brace-, comment- and verbatim-aware scanner. It
  produces changes, sections and markers.
- `ops.py`: pure accept and reject functions that return text edits.
- `author.py`: builds markup from anchors.
- `lint.py`: the compile traps, each reproduced against the Pop III preamble.
- `state.py`: journaled writes and undo. When nvim is running, it asks nvim to
  save first and reload afterwards, using a socket in `$XDG_RUNTIME_DIR`.
- `mcp_server.py`: the MCP tools. `rpc.py` serves the nvim plugin.
- `preview.py`: the PDF pane, built from synctex, pdftoppm, Pillow and `kitten icat`.
- `launcher.py`: the tmux layout.
- `nvim/`: the plugin.

Tests: `nix develop -c python -m pytest`. The LaTeX test needs `latexmk`. The
real-paper test runs when the Pop III paper is present.
