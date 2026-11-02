-- Message window: write to the agent pane from the paper, in one of three modes.
--   edit  (default) the agent changes the paper through the redline tools only
--   chat            talk only, the agent edits nothing
--   RAW             the agent may edit the .tex directly, untracked (dangerous)
-- The rules the agent follows are in redline/protocol.py; each message restates its mode.
local core = require("redline")
local tmux = require("redline.tmux")

local M = {}

local MODES = {
  { name = "edit", hl = "RedlineMsgEdit", title = " EDIT: tracked redline edits ",
    head = "[redline:edit] Instruction from the author. Carry it out ONLY with the redline tools"
      .. " (propose_*, revise_change, ...); never with plain_edit or generic file tools. If any part cannot be"
      .. " expressed as tracked markup (moving or resizing a figure, table layout, preamble, ...), say so in your"
      .. " FIRST line, before touching anything, and ask me to reply RAW if I want that part done as an untracked"
      .. " raw edit. I may be away: do the trackable parts meanwhile, and leave the rest until I reply." },
  { name = "chat", hl = "RedlineMsgChat", title = " CHAT: no edits ",
    head = "[redline:chat] Conversation only. Do not change the paper in any way. If I ask for a change,"
      .. " answer that edits go through the message window's edit mode (or RAW mode)." },
  { name = "RAW", hl = "RedlineMsgRaw", title = " ⚠ RAW EDIT: untracked, the .tex is changed directly ⚠ ",
    head = "[redline:RAW] RAW EDIT authorised for this message only. You may change the .tex directly"
      .. " (plain_edit with force=true, or file tools) with no tracked markup. Keep it minimal, compile() afterwards"
      .. " and list exactly what you changed. Nothing else is authorised." },
}

local function setup_hl()
  vim.api.nvim_set_hl(0, "RedlineMsgEdit", { default = true, link = "FloatBorder" })
  vim.api.nvim_set_hl(0, "RedlineMsgChat", { default = true, link = "DiagnosticInfo" })
  vim.api.nvim_set_hl(0, "RedlineMsgRaw", { default = true, fg = "#ffffff", bg = "#c0392b", bold = true })
end

--- The message the agent receives.
function M.format(mode, text, ctx)
  local m = MODES[1]
  for _, c in ipairs(MODES) do
    if c.name == mode then m = c end
  end
  local lines = { m.head }
  if ctx and ctx.line then
    table.insert(lines, ("Cursor: line %d of the paper%s."):format(ctx.line, ctx.section and (", §" .. ctx.section) or ""))
  end
  table.insert(lines, "")
  table.insert(lines, text)
  return table.concat(lines, "\n")
end

local function section_at(st, row)
  local best
  for _, s in ipairs(st and st.data and st.data.sections or {}) do
    local l = s.line or (s.lines and s.lines[1])
    if l and l <= row then best = s.title or s.name or best end
  end
  return best
end

function M.open(opts)
  opts = opts or {}
  local st = core.state()
  if not st then
    return vim.notify("redline: buffer not attached")
  end
  setup_hl()
  local row = vim.api.nvim_win_get_cursor(0)[1]
  local ctx = { line = row, section = section_at(st, row) }
  local idx = 1
  for i, m in ipairs(MODES) do
    if m.name == opts.mode then idx = i end
  end

  local buf = vim.api.nvim_create_buf(false, true)
  vim.bo[buf].bufhidden = "wipe"
  vim.bo[buf].filetype = "markdown"
  local w = math.min(90, vim.o.columns - 8)
  local h = math.min(12, vim.o.lines - 8)
  local win = vim.api.nvim_open_win(buf, true, {
    relative = "editor", width = w, height = h, row = math.floor((vim.o.lines - h) / 2) - 1,
    col = math.floor((vim.o.columns - w) / 2), style = "minimal", border = "rounded",
    title = MODES[idx].title, title_pos = "center",
    footer = " <C-s>/<CR> send · <Tab> mode · q/<Esc> cancel ", footer_pos = "center",
  })
  vim.wo[win].wrap = true

  local function paint()
    local m = MODES[idx]
    vim.wo[win].winhighlight = "FloatBorder:" .. m.hl .. ",FloatTitle:" .. m.hl
    vim.api.nvim_win_set_config(win, { title = m.title, title_pos = "center" })
  end
  local function close()
    if vim.api.nvim_win_is_valid(win) then vim.api.nvim_win_close(win, true) end
  end
  local function send()
    local text = vim.trim(table.concat(vim.api.nvim_buf_get_lines(buf, 0, -1, false), "\n"))
    if text == "" then
      return vim.notify("redline: empty message")
    end
    local m = MODES[idx]
    if m.name == "RAW" and vim.fn.confirm("RAW edit: the agent changes the .tex with no tracked markup. Send?",
        "&Send\n&Cancel", 2) ~= 1 then
      return
    end
    close()
    vim.cmd("stopinsert")
    tmux.send_agent(M.format(m.name, text, ctx))
  end
  local function cycle()
    idx = idx % #MODES + 1
    paint()
  end
  local function map(mode, lhs, fn)
    vim.keymap.set(mode, lhs, fn, { buffer = buf, nowait = true })
  end
  map({ "n", "i" }, "<C-s>", send)
  map("n", "<CR>", send)
  map({ "n", "i" }, "<Tab>", cycle)
  map("n", "q", close)
  map("n", "<Esc>", close)
  paint()
  vim.cmd("startinsert")
end

M.MODES = MODES
return M
