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
| `p` / `f` | show in the PDF pane / toggle follow. The PDF marks the change's own words with a highlighter, or the cursor line's words outside a change |
| `W` | pane layout preset (also `<leader>jw`; `<leader>jW` cycles) |
| `s` (also visual) | verify the claim on this line / the selection, via refcheck or code |
| `S` | send all open `% VERIFY` requests to the agent |
| `c` / `F` | compile (errors go to quickfix) / send the errors to the agent |
| `O` / `Q` | **sync to Overleaf**: push; merge Overleaf's new edits first (git, or an agent for conflicts) / close the sync output |
| `P` / `L` | pull+merge+push / pull (3-way merge) |
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

## Quitting

`prefix Q` in any pane of a redline session (or `<leader>jQ`, `:Redline quit`,
`redline quit`) asks first. A menu shows the windows, any files not yet on
Overleaf (compared with the last-sync snapshot, without downloading) and
whether a merge agent is still running. Quitting saves the nvim buffer, then
closes every window of the session. From nvim you also get a "Sync to Overleaf
first" button. Change the key with `tmux_quit_key` in `.redline.toml`, or set
it to `""` for none. The binding is global in tmux, but it does nothing
outside redline sessions.

## Pane layouts

`W` in review mode, `<leader>jw`, `:Redline layout <name>` or
`redline layout <name|--next|--prev>`. The panes are rearranged in place,
nothing restarts, and the PDF re-renders at the new size.

| preset | arrangement |
|---|---|
| `auto` | `pdf-tall` on a landscape window, `paper-top` on a portrait one (the default) |
| `default` | paper \| agent over PDF |
| `columns` | paper \| agent \| PDF |
| `rows` | paper / agent / PDF, stacked |
| `pdf-tall` | paper over agent \| full-height PDF (suits portrait pages) |
| `agent-bottom` | paper \| PDF, agent full width below |
| `pdf-left` | PDF \| paper \| agent |
| `paper-top` | paper on top, agent \| PDF below |
| `focus` | toggle the paper pane fullscreen (tmux zoom) |

Quitting with `prefix Q` (or `redline quit`) saves the exact arrangement of the
paper and code windows, including pane sizes you set by hand, to
`.redline/layout.json`. The next `redline open` of that paper restores it,
scaled to the terminal you open in. `redline save-layout` saves it without
quitting; deleting the file goes back to the preset.
Otherwise `layout = "..."` in `.redline.toml` picks the arrangement at start (default `auto`). To switch from
any pane, bind a key in `tmux.conf`, e.g.
`bind-key M-l run-shell "redline layout --next --session '#{session_name}'"`.
That needs `redline` on tmux's PATH.

## Verifying claims

Select a claim and press `s`, then pick refcheck (Zotero PDFs), code (the
analysis repo) or either. Redline adds a request line under the claim and,
unless you queue it, sends the agent a task with the context:

```latex
The sink accretes at $10^{-3}\,\Msun\,\mathrm{yr^{-1}}$ \citep{abel02}.
% VERIFY refcheck: «The sink accretes at 1e-3 Msun/yr»
```

The agent answers with `record_verification`, which replaces the request with
`% VERIFIED <bibkey | code:path:line>: <quote or value>`. A claim the source
contradicts gets `% REFUTED` plus a proposed correction; one it can't settle
gets `% UNVERIFIED <reason>`. These are the same lines refcheck already writes.
They show as ✓, ✗ and ? signs, and you can also type requests in Overleaf.
`redline verify` prints a report.

## Sync to Overleaf

`O` in review mode, `<leader>jo`, `:Redline sync` or `redline sync overleaf` do this:

1. Download Overleaf and compare it with the snapshot from the last sync. A
   file only counts as changed if Overleaf has something your copy doesn't.
   A push made outside redline (plain `ols -l`, `sync.sh` uploading plots) just
   refreshes the snapshot.
2. If one side is simply behind the other, update it without asking: push
   when only you have new edits, take Overleaf's version when only Overleaf
   has them (nothing to push then), and do nothing when both are the same.
3. If both sides have edits and they merge without conflicts, git merges them,
   then you confirm the push. Still no agent.
4. Only for real conflicts do you choose an agent. It works in `.redline/merge/`,
   which holds the file with conflict blocks and its `.LOCAL`, `.BASE` and
   `.REMOTE` versions. It must leave no markers and write `RESOLUTION.md` last.
   Redline checks the result, replays edits you made meanwhile and reloads nvim.
5. The push always lints and compiles first.

The output appears in one bottom split. Focus stays on the paper, `q`, `<Esc>`
or `Q` close it, and it closes by itself once you answer the push prompt.

Agents live under `[agents]`, in `~/.config/redline/config.toml` and/or the
paper's `.redline.toml`, with `{prompt}` replaced by the task. A plain list
runs headless. `{ cmd = [...], tui = true }` opens an interactive session in a
tmux window called `merge`, which you can watch and steer; it closes once the
merge is resolved. Defaults:

```toml
[agents]
claude = { cmd = ["claude", "--permission-mode", "acceptEdits", "{prompt}"], tui = true }
claude-headless = ["claude", "-p", "--permission-mode", "acceptEdits", "{prompt}"]
opencode = ["opencode", "run", "{prompt}"]
opencode-tui = { cmd = ["opencode", "--prompt", "{prompt}"], tui = true }
# "opencode-kimi" = ["opencode", "run", "-m", "kimi/kimi-k2", "{prompt}"]
```

On billing: Anthropic's help article "Use the Claude Agent SDK with your Claude
plan" (October 2026) says `claude -p` draws from the same subscription limits
as interactive use while you're signed in with your plan, unless an API key is
set. A planned split into a separate credit was paused. The interactive
default sidesteps the question either way.

The chat agent can do the same through MCP: `sync_to_overleaf(agent=...)`, or it
can resolve the workspace itself and then call `sync_finish`.

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
redline sync overleaf [--agent NAME]   # push if Overleaf unchanged, else merge with an agent
redline agents                # configured merge agents
redline verify [requests]     # verification report / the agent task for open requests
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
