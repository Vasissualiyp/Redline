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
to come from the system. `ols` (overleaf-sync, patched) is built from
`nix/overleaf-sync.nix` in this repo, so the flake has no external inputs
besides nixpkgs and flake-utils.
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
| `I` | **message window** to the agent (`<leader>ji`, `:Redline msg [edit\|chat\|RAW]`); `<Tab>` cycles the mode, `<C-s>`/`<CR>` sends. See below |
| `N` | send all `%N` notes to the agent in one message (`:Redline sendnotes`); a revise clears the `%N` so the change shows up as unrated again |
| `A` `R` `M` (also visual) | accept / reject / apply markers in the section, heading or selection |
| `K` / `gl` | details / list of all changes (Telescope) |
| `p` / `f` | show in the PDF pane / toggle follow. The PDF marks the change's own words with a highlighter, or the cursor line's words outside a change |
| `W` | pane layout preset (also `<leader>jw`; `<leader>jW` cycles) |
| `s` (also visual) | verify the claim on this line / the selection, via refcheck or code |
| `S` | send all open `% VERIFY` requests to the agent |
| `E` | evidence window for the `% VERIFIED id` line here (toggle) |
| `m` | agent pane: switch between Claude Code and opencode models (menu; also tmux prefix + `A`) |
| `c` / `F` | compile (errors go to quickfix) / send the errors to the agent |
| `T` | auto-compile on/off (compiles 10 s after the last edit) |
| `O` | **sync to Overleaf**: push; merge Overleaf's new edits first (git, or an agent for conflicts) |
| `Q` | close the sync output if it is open, otherwise close the whole redline session (asks first) |
| `P` / `L` | pull+merge+push / pull (3-way merge) |
| `q` `<Esc>` `i` | leave review mode |

While a buffer is in review mode the statusline shows a **REVIEW** mode: with
lightline.vim this happens by itself (the mode segment reads REVIEW, and it and the
line:column block turn yellow). Other
statuslines can use `require("redline.statusline").mode()` (`"REVIEW"` or nil), e.g.
lualine: `lualine_a = { { "mode", fmt = function(s) return require("redline.statusline").mode() or s end } }`;
a `User RedlineReview` autocmd fires on every switch.

Outside review mode:
- `]r` / `[r` jump between changes.
- `<leader>ja{motion}` and `<leader>jx{motion}` accept or reject a range, e.g.
  `<leader>jaip`. `<leader>jA` / `<leader>jX` act on the change under the cursor.
- `<leader>jd` asks the agent, `<leader>jc` compiles and `<leader>jr` toggles the raw markup.
- `:Redline {review,list,pending,notes,compile,fix,lint [fix],push,pull,acceptall}`.

To put the status in your statusline, use `require('redline').status()`
(e.g. `REVIEW C12/49 · §3.2 · 2 marked`).

Undo is ordinary vim undo. Accepting and rejecting are applied as buffer edits.

## Messaging the agent

`I` opens a small window with three modes (`<Tab>` switches, the border colour shows which):

| mode | what the agent may do |
|---|---|
| **edit** (default) | change the paper through the redline tools only (tracked markup) |
| **chat** | talk only; no edits |
| **RAW** (red, asks for confirmation) | change the `.tex` directly, untracked, for that message only |

Outside RAW the agent declines edits that bypass redline and points you to RAW mode. If an
edit cannot be tracked (moving or resizing a figure, table layout, ...) it says so in its
first line, before editing anything, and asks you to reply `RAW`; it does the trackable
parts meanwhile. Replying `RAW` in the agent pane allows a raw edit of exactly those parts.
The rules are in the MCP server instructions, so they hold for any agent.

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

## Restructuring

Splitting or reordering stays reviewable:

- **New list items and headings** are marked as `\item \added{…}` and
  `\subsection{\added{…}}`. A `\item` or heading inside `\added` breaks the
  build. A `%+item` / `%+heading` comment line tags inserted structure, so
  rejecting the change also removes its bullet or heading. A deleted item
  takes its bullet with it when accepted.
- **Moved text** (`propose_move`) is struck at the old place and added at the new
  one, each with a "Moved to / Moved here from" note.
- **`plain_edit`** (untracked) refuses prose changes of more than a few words.
  With `force` and a `reason` it leaves a visible `\chcomment`.

## Verifying claims

Select a claim and press `s`, then pick refcheck (Zotero PDFs), code (the
analysis repo) or either. Redline adds a request line under the claim and,
unless you queue it, sends the agent a task with the context:

```latex
The sink accretes at $10^{-3}\,\Msun\,\mathrm{yr^{-1}}$ \citep{abel02}.
% VERIFY refcheck: «The sink accretes at 1e-3 Msun/yr»
```

The agent answers with `record_verification`, giving the exact sentence(s) and
page from each source PDF. Redline checks that every quote is really on its page,
then replaces the request with a short id:

```latex
% VERIFIED abel02#1
% VERIFIED yoshida03#2 (+kulkarni21)
```

The evidence behind the id lives in Zotero and in a local cache
(`.redline/evidence.json`):

- each quote is highlighted in its PDF (green supports, red contradicts), tagged `rl:<id>`;
- a note per id, in the collection `Redline evidence/<paper>` (setting
  `evidence_collection`), holds the claim, verdict, sources, pages and quotes, and
  links to the highlights. Zotero sync carries it to your other machines, and
  `redline evidence sync` rebuilds the cache there (or files entries made while
  Zotero was closed).

On a result line (or the claim above it), `E` in review mode (`<leader>jv`,
`:Redline evidence`) opens a tmux window "evidence": the source page with the quote
highlighted (two pages stitched if it runs over a page break) and the note beside it.
In the PDF pane `n`/`p` switch sources, `]`/`[` switch between the claims whose result
lines are stacked in the same block (the note follows), and `o` opens the highlight in
Zotero; `q` in either pane, or `E` again, closes the window.

A claim the source contradicts gets `% REFUTED` plus a proposed correction; one it
can't settle gets `% UNVERIFIED nosrc#n` (the reason is in the library). Results show
as ✓, ✗ and ? signs, and you can also type requests in Overleaf. `redline verify`
prints a report, `redline evidence list|get ID|sync` work with the library, and
`redline evidence migrate [--apply]` moves old inline results
(`% VERIFIED key: "quote" — p.N`) into it; the dry run lists quotes it can't find.

## PDF pane keys

| key | action |
|---|---|
| `e` / Enter | jump to the LaTeX source of what is shown: nvim's cursor goes to the highlighted change, or to the first line of text in view, and tmux switches to the paper pane |
| `j`/`k`, space/`b`, `g`/`G` | next / previous / first / last page |
| `+` `-` `0` `z` | zoom in / out / reset to the whole page / crop around the highlight |
| `w` | whole page fitted to the pane (default) <-> page width, scrolled to the highlight (readable in a short pane) |
| `f` | follow the nvim cursor on/off |
| `r` / `q` | redraw / quit |

## Citation chains and pulled papers

When a quote only *names* a paper ("the more recent Faucher-Giguère (2020) model"),
the evidence source carries `for_key` (the cited .bib key). Redline then finds the
mention in that source's own reference list, highlights the entry in Zotero (orange),
shows every same-author-same-year entry, and compares it with the `.bib` entry by DOI,
arXiv id, journal volume and first page, or title. If none match, nothing is written:
the old `Faucher-Giguere_2019` key (a cosmic-ray paper, MNRAS 488, 3716) fails against
FIRE-3's entry "Faucher-Giguère C.-A., 2020, MNRAS, 493, 1614". Any cited paper that is
not in Zotero is pulled in with its PDF (DOI, then arXiv, then an open-access copy);
only a paywalled paper with no open copy stops the edit, and the message says so.

## Compiling by itself

Once the paper has been quiet for 10 seconds after an edit (yours, or an agent's),
it compiles by itself. Typing in insert mode does not start the countdown until
you leave insert mode, only one compile runs at a time (edits made during one queue
exactly one more), and a manual `c` still compiles right away. Errors show as
diagnostics on their lines and in the quickfix list; a message appears only when
the paper stops or starts compiling, and in review mode the statusline reads
`REVIEW TeX…` while compiling and `REVIEW TeX ✗` while it fails. The PDF pane only
ever shows the last PDF that compiled cleanly (`.redline/view/`). `T` in review mode
(`<leader>jt`, `:Redline autocompile`) turns it on or off; `auto_compile = 10` in the
plugin setup sets the delay (0 = off).

## PDF pane speed

Pages are rasterised with MuPDF (`mutool`, 10-20x faster than poppler on this
paper's figures), sent to kitty once as 256-colour PNGs (about a third of a
full-colour PNG; it all crosses ssh as base64) and then shown with kitty's unicode
placeholders. Kitty keeps the last 16 images, so going back to a page, or to one
sent ahead of time, only prints the placeholder text: the pages next to the one you
are looking at are sent while you read. `REDLINE_ICAT=1` falls back to `kitten icat`,
which re-sends every image.

## Switching the agent

`m` in review mode (`<leader>jm`, `:Redline agent`), or tmux prefix + `A` from any
pane, lists the agents and restarts the agent pane with the one you pick (the
conversation in it ends). The defaults:

| name | runs |
|---|---|
| `claude` | Claude Code |
| `deepseek` | opencode, `deepseek/deepseek-flash#high` |
| `gpt` | opencode, `openai/gpt-6-sol#high` (greyed out until OpenAI is set up: `opencode auth login`) |
| `qwen-local` | opencode, `ollama/qwen3.8` on vas-desktop (greyed out when the desktop is unreachable) |

Picking an opencode entry writes its model into the paper's `opencode.json`; both
agents' MCP configs (`.mcp.json`, `opencode.json`) always list the redline and
zotero servers, and `redline open` starts the last choice. Add or change entries in
`.redline.toml` or `~/.config/redline/config.toml`:

```toml
[models.kimi]
label = "opencode: Kimi K3"
cmd = "opencode --standalone"
model = "kimi/kimi-k3"
```

`redline agent` lists them from the shell, `redline agent NAME` switches.

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
redline evidence list|get ID|show ID|sync|migrate [--apply]   # evidence behind % VERIFIED ids
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

## All your papers: `redline full`

```
redline full                 # full-screen list of every paper (Textual)
redline full list [--json]   # name, pending, unrated, Overleaf state, tmux, last edit
redline full remote          # the projects on your Overleaf account
redline full new NAME        # create the project on Overleaf and a local paper with a starter main.tex
redline full new NAME --pull # pull the existing Overleaf project of that name instead
redline full open NAME       # start (or attach to) the paper's tmux workspace
```

Papers are the directories under `roots` (default `~/Software/Overleaf-nix-flake`) that have a
`.redline.toml`; set `roots` and `papers_dir` (where new papers go) in `~/.config/redline/config.toml`.
The TUI moves like vim: `j k gg G H M L ctrl-d ctrl-u ctrl-e ctrl-y`, `/` filters (enter keeps the filter, esc clears).
Papers carry the name they have on Overleaf. Papers: `enter`/`o` open the tmux workspace, `l` review its changes,
`n` new (type to filter your Overleaf projects, enter pulls the highlighted one, ctrl+o creates a new project after asking),
`p` pull, `P` push (asks first), `c` compile, `r` refresh, `q` quit.

**Quick menu** (`l` on a paper, `h` back): every pending change with the PDF page that holds it, highlighted.
`a` accept, `x` reject (journaled, `u` undoes), `n` note for the agent (kept pending as `%N`; the agent revises it),
`d` ask the agent without editing, `t` talk to the agent about the paper (also on the paper list), `enter` opens the
editor at the change, `c` compile, `v` switches between the PDF page and the **text view**: the source around the change
with green added, red struck-through deleted, cyan comments and the chosen change in bold (no kitty graphics needed;
it is the default on terminals without them). The agent is the one in the
paper's tmux session: it is started in the background when you begin writing a message (the list shows it as
running) and reused afterwards. The PDF is rebuilt by itself 10 s after the paper last changed (also for edits from the agent or the editor).
While it is older than the source, or the paper does not compile, no highlight is drawn (it would land on the wrong
line) and the reason is shown instead, e.g. the first LaTeX error.
The right side shows the first page of the highlighted paper's last build (kitty graphics, like the PDF pane;
`REDLINE_PREVIEW=0` turns it off), so papers are recognisable at a glance. Papers never compiled say so; press `c`.

A paper without `--code-dir` gets an `analysis/` directory for its code, kept out of Overleaf.

Setting a paper up (`redline init`, `redline full new`, the first `redline open`) also adds what the markup
needs to the preamble if missing: the `changes` package and a `\providecommand{\chcomment}{\comment}` alias
(`changes` only defines `\chcomment` when another package already owns `\comment`).

## Turning refcheck off for a project

Refcheck is on by default: new citations need evidence from the cited paper's PDF (checked against Zotero) and
leave a `% VERIFIED` line. A project that is not about cited literature turns it off **at creation**
(`redline init --no-verification`, answering `n` when `redline init` asks, `redline full new NAME --no-verification`,
or `n` in the new-paper dialog of `redline full`), and can change it later: `redline verification on|off|status`
(it is `enforce_verification` in `.redline.toml`). Off means citations need no evidence, `plain_edit` may add them
and lint stays quiet; `% VERIFY` requests you write yourself are still answered. Agents are told never to change it.

## Approving everything at once

`gA` in review mode (`:Redline acceptallforce`), `A` in the `redline full` quick menu / paper list, and
`redline accept --all --ignore-markers` accept every edit whatever its `%Y/%N/%X` marker says. They all show how many
`%N`/`%X` they will override and ask first (`--yes` skips the CLI question). Standalone comments stay; undo works.

## Progress indicators

Anything slow shows an animated spinner (⠋⠙⠹…): in `redline full` a yellow bar above the log and a Status cell on the
paper's row (compile, pull, push, reading papers/changes, starting the tmux session, sending to the agent), a placeholder
row for a paper being created or pulled, "rendering the page…" in the picture pane, and "loading the Overleaf project
list…" in the new-paper dialog. A job that ends at once still shows for ~0.8 s. In nvim the statusline's mode label
animates while compiling (auto or manual) or syncing with Overleaf. On the command line `redline compile`,
`redline init` and `redline full new` show a spinner on stderr (only on a terminal).

## Categories, the edit screen and coming back

`redline full` lists papers as leaves of a tree of categories you define (`~/.config/redline/categories.json`;
nested with `/`: `Papers/Pop III`; their order and colour are yours; papers without one sit under *Uncategorized*, and with
no categories at all the list stays flat). On the paper list: `h`/`l`/`enter` close and open a category (`h` on a
paper jumps to its category), `C` new category (the cursor's category is the default parent; the colour is picked from swatches, or typed as a name or `#rrggbb`), `e` edit (a category: name
and colour; a paper: **category (with "+ New category…" in the list), strict refchecks on/off, code directory, notes, Overleaf project name**, `ctrl+s` saves;
`e` also works inside a paper's quick menu), `J`/`K` move a category among its siblings or a paper within its category,
`D` delete a category (its papers become uncategorized). A paper's category is the `category` key of its `.redline.toml`.
From the shell: `redline full categories | cat-add NAME --color C | cat-color NAME C | cat-rm NAME |
set PAPER key=value`.

**Back to the list from a paper's editor:** `gh` in review mode (`<prefix>H`, `:Redline full`) switches the tmux client
back to the window running `redline full`, or starts a `redline-full` session if it isn't running (`redline full back`).

## Agents and the other papers

An agent in one paper can read the others in your `redline full` list, read-only: `papers()` (name, category, pending
changes, notes), `paper_outline`, `paper_read`, `paper_changes` and `paper_search` (one paper or all). Edits to another
paper still go through that paper's own session. `cross_paper_access = false` in a paper's `.redline.toml` (a switch in its
edit screen, or `redline full set PAPER cross_paper_access=false`) makes it private in both directions: its agent sees no
other paper and no other agent sees it. Restart the agent (or its MCP server) once to pick up the new tools.

## Single-editor papers: automatic Overleaf sync

`single_editor = true` (edit screen, `redline full set PAPER single_editor=true`, or `.redline.toml`) says nobody else
edits the paper, so it syncs with Overleaf by itself instead of on demand. In nvim and in the `redline full` quick menu,
`auto_sync_seconds` (default 20) after the last edit (yours, or an agent's) a background `redline sync auto` runs: push if
Overleaf is unchanged; take Overleaf's edits when it is ahead, merging first when both sides changed without conflicts.
Every `auto_sync_poll` seconds (default 120) it also looks for edits made on Overleaf itself. Nothing blocks the editor;
the statusline shows an animated "syncing". It never resolves a conflict and never pushes lint or compile errors (it says
so once); it skips the recompile when a clean build of exactly this text exists, and a lock keeps two syncs from running at
once. `redline sync auto [--json]` is the same step from the shell (exit 3: needs you, 4: another sync running).

## Two sessions for one paper

`two_sessions = true` (edit screen, `redline full set PAPER two_sessions=true`, or `.redline.toml`) starts two tmux
sessions: `redline-NAME` with nvim + the agent (and the code window), and `redline-NAME-pdf` with only the PDF view, for a
second monitor or terminal: `redline open` attaches the first, `redline open --pdf` the second. It is the same preview
process and socket a pane would have, so everything the editor does to the PDF (following the cursor, highlights,
recompiles, `e` to jump to the source) works exactly as in one window; the jump selects the editor's window in the other
session. Quitting closes both, from either side: `\jQ` / `prefix Q` / `redline quit`; the confirmation lists the windows of
both. Layout presets leave the missing PDF pane out.

One-off, without changing the paper's setting: `redline open --two-sessions` (or `redline full open NAME --two-sessions`;
`--one-session` forces a single session), and in `redline full` **shift+enter** (or `O`, for terminals that cannot tell
shift+enter from enter) opens the paper in two sessions; the list marks such papers `running ×2`.

**Switching while it runs:** `gS` in review mode (`:Redline sessions [one|two|toggle]`, or `redline sessions` from a shell;
`--save` also stores the choice as the paper's setting) moves the PDF pane out into a new `-pdf` session, or joins it back
into the editor window and closes the PDF session. The preview process is moved, not restarted, so nothing is re-rendered
or lost. Going to two sessions only creates the second one; attach it yourself with `redline open --pdf`.

When the quit menu offers "Sync to Overleaf first" and you accept, the session closes by itself once everything is on Overleaf
(already in sync, pushed, or merged and pushed). If the sync stops short (a conflict, a refused or declined push), the
session stays open and says so.

## The `?` key help

`?` in review mode opens a grouped, colour-coded key list: move (blue), accept/reject (green), marks (yellow), edit
(orange), agent (magenta), verify (cyan), compile/PDF (red), Overleaf (teal), session (grey); `v` before a key means visual
mode. It is an ordinary buffer, so `j k <C-d> <C-u> <C-e> <C-y> gg G H M L` scroll it. `/` filters as you type (several
words are ANDed and match the key, its text or its group; `<CR>` keeps the filter, `<Esc>` clears it), `<Tab>`/`<S-Tab>`
show one group at a time, `q`, `?` or `<Esc>` close. Colours are the highlight groups `RedlineHelpMove`, `RedlineHelpDecide`,
`RedlineHelpMark`, `RedlineHelpEdit`, `RedlineHelpAgent`, `RedlineHelpVerify`, `RedlineHelpBuild`, `RedlineHelpSync`,
`RedlineHelpSession`: override them in your colorscheme.

## Recompiles do not move the page you are reading

The PDF is rebuilt in the background as the paper changes, but the page on screen stays exactly as it is until you move on:
the cursor reaches another change (nvim sends a new place), you page (`j k space b g G`), zoom, or redraw (`r`). The status
line says "newer build ready: shown when you move on". The pane keeps its own copy of the build it shows (with its synctex
file), so a resize redraws the same build. The quick menu in `redline full` works the same way: a finished compile refreshes
the page only when you go to another change (or when the preview was showing a note such as "out of date").

`N` (send all `%N` notes) only sends markers that carry note text; a bare `%N` (keep pending, nothing to say) is left out,
and the confirmation says how many were skipped.

## High-level overview (`gO`)

`gO` in review mode (`:Redline overview [tab]`) opens the paper as a tree of one-line **thoughts**: sections, then one
node per paragraph (¶), figure (▣), table (▦), list (≡) with its items (•), equation (∑) and the abstract (A). Headings show their number as typeset (`3.4.1  Title`). Each row has fixed columns first, **l w c** (lines, words, chars, right-aligned so sizes line up; a section shows its totals), then the thought; `gc` hides / shows those columns. It sits in a sidebar by default; `s`
toggles a full tab. Moving the cursor in the overview scrolls the paper to the block, highlights it there and **highlights the whole block
in the PDF pane**; and the other way round, moving through the paper moves the overview to the block your cursor is in
(the nearest block above on a blank line, the section heading when the block is folded away) and highlights its row.

- **Hashes:** every block is hashed from its *accepted* text (pending `\added/\deleted/\replaced` resolved, comments, review
  markers and `% VERIFIED` lines ignored). Edit a paragraph and its thought turns stale: still shown, dimmed and marked `~`
  (it is carried over from the old wording), until refreshed. Accepting or rejecting an edit that changes nothing in the
  reading text does not invalidate it. Everything is in `.redline/outline.json`, outside the paper.
- **Thoughts** are written by an agent through the MCP tools `outline_pending` / `record_thoughts`, with the model *you* pick for
  this job: `outline_model` in `.redline.toml` is an entry of `[models]` (default `deepseek`; `qwen-local`, `claude`, or
  `pane` to use the agent pane). `gt` runs it headless for the missing and stale blocks only (one batch); `gT` goes through the
  agent pane instead, `gW` rewrites every thought. `redline overview refresh [--model NAME]` does the same from a shell.
- **Flow check:** `gF` asks the model to read the *sequence of thoughts* (not the text) and flag real problems: gap, repeat,
  forward reference, weak transition, orphan figure. Flags show as red lines under the nodes (`]f` `[f` jump between them) and
  vanish when the blocks involved change.
- **Rearranging works like Oil:** the overview buffer is editable (number and relative numbers are on, every line has a
  hidden id). `dd` a line (`50dd`, a visual block, or a folded section, which moves as a whole) and `p` it somewhere else
  to **move** it; `dd` without pasting **deletes** the block; `:w` commits what you did into a draft plan (moved blocks
  show `⇅`, the header counts moves and deletions; `u` undoes as usual, `gr` discards the plan). The paper is untouched
  until `gA`, which offers two ways: **directly** (declaratively, no agent: every move becomes a tracked
  `\deleted` here + `\added` there with a "Moved to / Moved here from" footnote, every deletion a tracked deletion, all in
  one write you undo with one `u`; blocks holding pending changes or other things that cannot be done mechanically are skipped
  and listed) or **hand it to the agent** (tracked `propose_move` / `propose_delete`, and it may add a transition).
- **Merge / split:** `m` marks blocks, `gm` asks the agent to merge the marked ones (or this one and the next) into one paragraph,
  `gs` to split the block under the cursor into several; both take a free-text instruction and produce tracked edits. The new
  paragraphs get fresh thoughts on the next `r`.
- `?` inside lists the keys.

## The agent and the analysis code

`code_dir` was only the directory of the "code" tmux window, so an agent could not know where the plotting code is. Now the
agent's instructions name it (when the paper has a `code_dir`), `status()` shows it, and four read-only tools work whatever the
agent's sandbox says: `code_dir()` (location, top level, plotting scripts), `code_list(glob)`, `code_read(path, lines)`,
`code_search(query)`. They cannot leave the directory (paths and symlinks are resolved first). A `code_dir` on a network
mount (cluster scratch over sshfs) that has gone stale would hang forever; Redline answers "does not answer within 6 s" instead
of freezing, and `redline open` starts the code window in the paper directory. Restart the agent to pick up the new tools.

The PDF highlight of a block covers the whole block however long it is: the overview sends all of its text, the pane asks
synctex about up to 24 lines spread over it, and a block that runs over several pages is searched on all of them (up to eight);
the pane shows its first two pages stitched.

### Recommendations in the overview

- **What waits for you:** a gutter after l/w/c shows, per paragraph, `✎N` pending edits (orange: some undecided, magenta:
  `%N` waiting on the agent, red: marked `%X`, green: all `%Y`) and `✉N` comments (cyan; a change's own explanation counts).
  Sections sum their blocks; the header says how many blocks need review.
- **✂ split recommended:** while writing thoughts the model may flag a long block (`outline_split_words`, default 200) that holds
  two or more *separate*, individually substantial ideas (e.g. a long paragraph describing two figure panels at length). A short
  paragraph is never flagged, nor related ideas that belong together (two related panels in 100 words). The parts show under the
  row; `gs` pre-fills its split request with them; `]s` `[s` jump between them.
- **Dismissing:** `gd` dismisses the recommendations on a block; they stay away until that block is run through the model again
  (`gR`, which rewrites its thought). A paragraph you edit gets a stale thought and loses its recommendation until refreshed.
- **Structure review:** once every thought is up to date (`outline_review = true`, or `gv`), the same cheap model reads all the
  thoughts together, knowing the split flags, and recommends **moves** and **merges** (`⇢` lines). `ga` accepts one (a move goes into
  your draft plan, a merge marks the blocks for `gm`), `gd` dismisses it.
- **Full reclassification:** `gW` / `gC` (after a confirmation screen listing what is dropped) runs every block through the model
  again and resets everything that depends on the thoughts (recommendations, flow flags, dismissals); use it after changing how
  thoughts are written. Your draft plan and marks stay.

**Undo in the overview** (`u`, `<C-r>`) works in layers: edits you have not committed yet (the buffer's own undo), then
each plan commit (`:w`), `ga` and `gd`-reset step, and finally a plan you applied directly to the paper (it asks, then reverts that
one write, like `redline undo`). A reload or commit is never an undo step of its own.
