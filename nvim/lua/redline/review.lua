-- Review mode, navigation and accept/reject actions.
local core = require("redline")
local rpc = require("redline.rpc")
local tmux = require("redline.tmux")
local preview = require("redline.preview")

local M = {}

local function cur()
  local buf = vim.api.nvim_get_current_buf()
  return buf, core.bufs[buf]
end

local function cursor()
  local p = vim.api.nvim_win_get_cursor(0)
  return p[1] - 1, p[2]
end

local function items(st)
  return (st and st.data and st.data.items) or {}
end

local function goto_item(it)
  vim.api.nvim_win_set_cursor(0, { it.range[1] + 1, it.range[2] })
  vim.cmd("normal! zv")
  local _, st = cur()
  if st and st.review and core.config.follow then
    preview.show(st.root, it.lines[1], false, preview.excerpt(it), it.lines[2])
  end
end

--- Item under the cursor, or the first one starting on this line.
function M.current(strict)
  local buf, st = cur()
  if not st then
    return nil
  end
  local row, col = cursor()
  local it = core.item_at(buf, row, col)
  if it or strict then
    return it
  end
  for _, x in ipairs(items(st)) do
    if x.full_range[1] <= row and row <= x.full_range[3] then
      return x
    end
  end
end

function M.next(pred)
  local _, st = cur()
  local row, col = cursor()
  for _, it in ipairs(items(st)) do
    local r = it.range
    if (r[1] > row or (r[1] == row and r[2] > col)) and (not pred or pred(it)) then
      return goto_item(it)
    end
  end
  vim.notify("redline: no further changes", vim.log.levels.INFO)
end

function M.prev(pred)
  local _, st = cur()
  local row, col = cursor()
  local list = items(st)
  for i = #list, 1, -1 do
    local it, r = list[i], list[i].range
    if (r[1] < row or (r[1] == row and r[2] < col)) and (not pred or pred(it)) then
      return goto_item(it)
    end
  end
  vim.notify("redline: no earlier changes", vim.log.levels.INFO)
end

local function unrated(it)
  return it.marker == nil
end

-- ---- operations -------------------------------------------------------------
local function run_op(params, after)
  local buf, st = cur()
  if not st then
    return vim.notify("redline: buffer not attached (:Redline start)", vim.log.levels.WARN)
  end
  params.text = core.buf_text(buf)
  local res, err = rpc.call(st.root, params.method or "op", params)
  if err then
    return vim.notify("redline: " .. err, vim.log.levels.ERROR)
  end
  core.apply_edits(buf, res.edits)
  core.refresh(buf, after)
  return res
end

local function summary(report)
  local counts = {}
  for _, r in ipairs(report or {}) do
    local k = r.action .. (r.reason ~= "" and (" (" .. r.reason .. ")") or "")
    counts[k] = (counts[k] or 0) + 1
  end
  local parts = {}
  for k, v in pairs(counts) do
    table.insert(parts, v .. " " .. k)
  end
  return table.concat(parts, ", ")
end

--- Accept/reject the change under the cursor, then move to the next one.
function M.resolve_current(action)
  local it = M.current()
  if not it then
    return vim.notify("redline: no change under the cursor", vim.log.levels.WARN)
  end
  local row = it.range[1]
  run_op({ action = action, selector = { ids = { it.id } }, explicit = true, honor_markers = false },
    function()
      vim.api.nvim_win_set_cursor(0, { math.min(row + 1, vim.api.nvim_buf_line_count(0)), 0 })
      M.next()
    end)
end

function M.mark_current(kind)
  local it = M.current()
  if not it then
    return vim.notify("redline: no change under the cursor", vim.log.levels.WARN)
  end
  local function go(note)
    if note == nil then
      return
    end
    local clear = it.marker and it.marker.kind == kind and note == ""
    run_op({ method = "set_marker", id = it.id, kind = (not clear) and kind or vim.NIL, note = note },
      function() M.next(unrated) end)
  end
  if kind == "N" then
    vim.ui.input({ prompt = "%N note for " .. it.label .. " (empty = none): ",
      default = it.marker and it.marker.note or "" }, go)
  else
    go("")
  end
end

--- Selector for a range op: visual lines, a heading line, or the current section.
local function range_selector(visual)
  local buf, st = cur()
  if visual then
    local a, b = vim.fn.line("v"), vim.fn.line(".")
    if a > b then
      a, b = b, a
    end
    return { lines = { a, b } }, ("lines %d-%d"):format(a, b)
  end
  local row = cursor() + 1
  local best
  for _, s in ipairs((st.data and st.data.sections) or {}) do
    if s.line == row then
      return { section = s.number ~= "" and s.number or s.title }, "§" .. s.label
    end
    if s.line <= row and row <= s.end_line and (not best or s.level > best.level) then
      best = s
    end
  end
  if best then
    return { section = best.number ~= "" and best.number or best.title }, "§" .. best.label
  end
  return { all = true }, "the whole paper"
end

function M.resolve_range(action, visual)
  local sel, what = range_selector(visual)
  if visual then
    vim.api.nvim_feedkeys(vim.api.nvim_replace_termcodes("<Esc>", true, false, true), "nx", false)
  end
  local verb = ({ accept = "Accept", reject = "Reject", markers = "Apply %Y/%X markers in" })[action]
  if vim.fn.confirm(("%s %s? (%%N stays pending, %%X is rejected)"):format(verb, what), "&Yes\n&No", 2) ~= 1 then
    return
  end
  local res = run_op({ action = action, selector = sel })
  if res then
    vim.notify("redline: " .. (summary(res.report) ~= "" and summary(res.report) or "nothing to do"))
  end
end

function M.accept_all()
  if vim.fn.confirm("Accept every change in the paper (honouring %N/%X)?", "&Yes\n&No", 2) == 1 then
    local res = run_op({ action = "accept", selector = { all = true } })
    if res then
      vim.notify("redline: " .. summary(res.report))
    end
  end
end

--- Operator: <prefix>a{motion}
function M.operator(action)
  M._op_action = action
  vim.go.operatorfunc = "v:lua.require'redline.review'._opfunc"
  return "g@"
end

function M._opfunc()
  local a, b = vim.fn.line("'["), vim.fn.line("']")
  local res = run_op({ action = M._op_action, selector = { lines = { a, b } } })
  if res then
    vim.notify("redline: " .. (summary(res.report) ~= "" and summary(res.report) or "nothing in range"))
  end
end

-- ---- revise in a float -------------------------------------------------------------
function M.edit_current()
  local it = M.current()
  if not it then
    return
  end
  local field = it.kind == "comment" and "comment" or (it.new and "new" or "old")
  local text = it[field] or ""
  local src_buf = vim.api.nvim_get_current_buf()
  local b = vim.api.nvim_create_buf(false, true)
  vim.bo[b].filetype = "tex"
  vim.bo[b].buftype = "acwrite"
  vim.api.nvim_buf_set_name(b, "redline://" .. it.id)
  vim.api.nvim_buf_set_lines(b, 0, -1, false, vim.split(text, "\n", { plain = true }))
  local width = math.min(100, vim.o.columns - 6)
  local win = vim.api.nvim_open_win(b, true, {
    relative = "cursor", row = 1, col = 0, width = width, height = math.min(12, math.floor(#text / width) + 3),
    border = "rounded", style = "minimal",
    title = (" revise %s (%s) — :w applies, q cancels "):format(it.label, field == "old" and "becomes \\replaced" or field),
  })
  vim.wo[win].wrap = true
  vim.keymap.set("n", "q", "<cmd>close<CR>", { buffer = b })
  vim.api.nvim_create_autocmd("BufWriteCmd", {
    buffer = b,
    callback = function()
      local new = table.concat(vim.api.nvim_buf_get_lines(b, 0, -1, false), "\n")
      vim.api.nvim_win_close(win, true)
      vim.api.nvim_set_current_buf(src_buf)
      local params = { method = "revise", id = it.id }
      if field == "comment" and it.kind ~= "comment" then
        params.comment = new
      else
        params.new_text = new
      end
      run_op(params)
      vim.bo[b].modified = false
    end,
  })
end

function M.edit_comment()
  local it = M.current()
  if not it or it.kind == "comment" then
    return M.edit_current()
  end
  vim.ui.input({ prompt = "comment for " .. it.label .. ": ", default = it.comment or "" }, function(c)
    if c and c ~= "" then
      run_op({ method = "revise", id = it.id, comment = c })
    end
  end)
end

-- ---- talking to the agent -------------------------------------------------------------
function M.discuss()
  local it = M.current()
  vim.ui.input({ prompt = it and ("Ask the agent about " .. it.label .. ": ") or "Ask the agent: " }, function(q)
    if not q or q == "" then
      return
    end
    local msg
    if it then
      msg = ("[redline] About change %s (id %s, %s, §%s, lines %d-%d, origin %s):\n"):format(
        it.label, it.id, it.kind, it.section, it.lines[1], it.lines[2], it.origin)
      if it.new then msg = msg .. "new: «" .. it.new .. "»\n" end
      if it.old then msg = msg .. "old: «" .. it.old .. "»\n" end
      if it.comment then msg = msg .. "comment: «" .. it.comment .. "»\n" end
      msg = msg .. q .. "\n(Refer to the change by its id; use the redline MCP tools for edits.)"
    else
      local row = cursor() + 1
      msg = ("[redline] About line %d of the paper: %s"):format(row, q)
    end
    tmux.send_agent(msg)
  end)
end

local function clip(s, n)
  s = (s or ""):gsub("%s+", " ")
  return #s <= n and s or (s:sub(1, n - 1) .. "…")
end

--- Send every %N note to the agent in one message (one turn, one compile).
function M.send_notes()
  local st = core.state()
  if not (st and st.data) then
    return vim.notify("redline: buffer not attached")
  end
  local noted = vim.tbl_filter(function(it) return it.marker and it.marker.kind == "N" end, st.data.items)
  local orphans = vim.tbl_filter(function(m) return m.kind == "N" end, st.data.orphan_markers or {})
  local n = #noted + #orphans
  if n == 0 then
    return vim.notify("redline: no %N notes in the paper")
  end
  if vim.fn.confirm(("Send %d %%N note(s) to the agent?"):format(n), "&Yes\n&No", 1) ~= 1 then
    return
  end
  local lines = {
    ("[redline] Review notes: I marked %d pending change(s) with %%N. For each one, address the note"
      .. " with revise_change(id, new_text?, comment?) (that clears the %%N so I re-review it), or"
      .. " answer with revise_change(id, comment=\"Kept: <why>\") if you disagree. Do any checks"
      .. " yourself (read the section or the code), don't stack new suggestions, compile() once at"
      .. " the end, then give me one line per change. Full text: list_changes({\"ids\": [...]}, full=true)."):format(n),
    "",
  }
  for _, it in ipairs(noted) do
    table.insert(lines, ("- %s id %s (%s, §%s, l.%d): note «%s»"):format(
      it.label, it.id, it.kind, it.section, it.lines[1], it.marker.note ~= "" and it.marker.note or "(no text)"))
    if it.new then table.insert(lines, "    new: «" .. clip(it.new, 240) .. "»") end
    if it.old then table.insert(lines, "    old: «" .. clip(it.old, 160) .. "»") end
  end
  for _, m in ipairs(orphans) do
    table.insert(lines, ("- line %d (not on a change): note «%s»"):format(m.line, m.note))
  end
  tmux.send_agent(table.concat(lines, "\n"))
end

function M.send_visual()
  local a, b = vim.fn.line("v"), vim.fn.line(".")
  if a > b then a, b = b, a end
  vim.api.nvim_feedkeys(vim.api.nvim_replace_termcodes("<Esc>", true, false, true), "nx", false)
  local lines = vim.api.nvim_buf_get_lines(0, a - 1, b, false)
  vim.ui.input({ prompt = ("Ask the agent about lines %d-%d: "):format(a, b) }, function(q)
    if q and q ~= "" then
      tmux.send_agent(("[redline] About lines %d-%d of the paper:\n%s\n\n%s"):format(a, b, table.concat(lines, "\n"), q))
    end
  end)
end

-- ---- build / sync ----------------------------------------------------------------------
M.last_compile = nil

function M.compile(cb)
  local _, st = cur()
  if not st then
    return
  end
  core.flush()
  vim.notify("redline: compiling…")
  vim.system(rpc.shell("compile --json"), { cwd = st.root, text = true, env = { REDLINE_ROOT = st.root } },
    vim.schedule_wrap(function(r)
      local ok, res = pcall(vim.json.decode, r.stdout or "", { luanil = { object = true, array = true } })
      if not ok or type(res) ~= "table" then
        return vim.notify("redline compile: " .. (r.stderr or r.stdout or "?"), vim.log.levels.ERROR)
      end
      M.last_compile = res
      local qf = {}
      local main = vim.api.nvim_buf_get_name(vim.api.nvim_get_current_buf())
      for _, e in ipairs(res.errors or {}) do
        table.insert(qf, { filename = main, lnum = math.max(e.line, 1), type = "E",
          text = (e.item ~= "" and ("[" .. e.item .. "] ") or "") .. e.message })
      end
      for _, u in ipairs(res.undefined or {}) do
        table.insert(qf, { filename = main, lnum = math.max(u.line, 1), type = "W",
          text = "undefined " .. u.kind .. " " .. u.key })
      end
      vim.fn.setqflist(qf, "r")
      vim.fn.setqflist({}, "a", { title = "redline compile" })
      if res.ok then
        vim.notify(("redline: compiled in %.0fs%s"):format(res.seconds,
          #(res.undefined or {}) > 0 and (" (" .. #res.undefined .. " undefined refs)") or ""))
      else
        vim.notify(("redline: compile failed, %d error(s) in quickfix; %s sends them to the agent")
          :format(#(res.errors or {}), core.config.prefix .. "F"), vim.log.levels.ERROR)
      end
      if cb then cb(res) end
    end))
end

function M.fix_with_agent()
  local res = M.last_compile
  if not res or res.ok then
    return vim.notify("redline: no failed compile to fix (compile first)")
  end
  local lines = { "[redline] The paper fails to compile. Errors (line: message [change]):" }
  for _, e in ipairs(res.errors or {}) do
    table.insert(lines, ("  l.%d: %s%s%s"):format(e.line, e.message,
      e.item ~= "" and (" [" .. e.item .. " " .. e.item_id .. "]") or "",
      e.context ~= "" and ("  | " .. e.context) or ""))
  end
  table.insert(lines, "Run lint(fix=true) first; then fix the rest with revise_change/plain_edit and re-run compile() until it passes.")
  tmux.send_agent(table.concat(lines, "\n"))
end

local function sync(action, confirm_msg)
  local _, st = cur()
  if not st then
    return
  end
  if confirm_msg and vim.fn.confirm(confirm_msg, "&Yes\n&No", 2) ~= 1 then
    return
  end
  core.flush()
  vim.notify("redline: " .. action .. "…")
  vim.system(rpc.shell("sync " .. action), { cwd = st.root, text = true, env = { REDLINE_ROOT = st.root } },
    vim.schedule_wrap(function(r)
      local out = ((r.stdout or "") .. (r.stderr or "")):gsub("%s+$", "")
      vim.notify("redline " .. action .. ":\n" .. out, r.code == 0 and vim.log.levels.INFO or vim.log.levels.WARN)
      core.reload()
    end))
end

function M.push()
  sync("push", "Pull+merge, compile and push the paper to Overleaf?")
end

function M.pull()
  sync("pull")
end

-- ---- review mode -------------------------------------------------------------------------
local REVIEW_KEYS = {
  { "n", "]c", function() M.next() end, "next change" },
  { "n", "[c", function() M.prev() end, "previous change" },
  { "n", "]n", function() M.next(unrated) end, "next unrated change" },
  { "n", "[n", function() M.prev(unrated) end, "previous unrated change" },
  { "n", "a", function() M.resolve_current("accept") end, "accept (then next)" },
  { "n", "x", function() M.resolve_current("reject") end, "reject (then next)" },
  { "n", "n", function() M.mark_current("N") end, "keep pending: %N + note for the agent" },
  { "n", "y", function() M.mark_current("Y") end, "mark %Y (accept later in bulk)" },
  { "n", "X", function() M.mark_current("X") end, "mark %X (reject later in bulk)" },
  { "n", "e", M.edit_current, "edit the suggested text" },
  { "n", "C", M.edit_comment, "edit/add the explaining comment" },
  { "n", "d", M.discuss, "ask the agent about this change" },
  { "n", "N", M.send_notes, "send all %N notes to the agent (one message)" },
  { "x", "d", M.send_visual, "ask the agent about the selection" },
  { "n", "p", function() preview.show_cursor(core.state().root, true) end, "show in PDF pane" },
  { "n", "A", function() M.resolve_range("accept", false) end, "accept section (or heading under cursor)" },
  { "x", "A", function() M.resolve_range("accept", true) end, "accept selection" },
  { "n", "R", function() M.resolve_range("reject", false) end, "reject section" },
  { "x", "R", function() M.resolve_range("reject", true) end, "reject selection" },
  { "n", "M", function() M.resolve_range("markers", false) end, "apply %Y/%X markers in section" },
  { "x", "M", function() M.resolve_range("markers", true) end, "apply %Y/%X markers in selection" },
  { "n", "gl", function() require("redline.picker").pick() end, "list changes" },
  { "n", "K", function() require("redline.picker").details(M.current()) end, "change details" },
  { "n", "c", function() M.compile() end, "compile" },
  { "n", "F", M.fix_with_agent, "send compile errors to the agent" },
  { "n", "s", function() require("redline.verify").request(false) end, "verify the claim on this line (code/refcheck)" },
  { "x", "s", function() require("redline.verify").request(true) end, "verify the selected claim (code/refcheck)" },
  { "n", "S", function() require("redline.verify").send_all() end, "send all open % VERIFY requests to the agent" },
  { "n", "E", function() require("redline.verify").evidence() end, "evidence window for the % VERIFIED line here (toggle)" },
  { "n", "O", function() require("redline.sync").sync() end, "sync to Overleaf (push; merge first if Overleaf has edits)" },
  { "n", "Q", function()
    local sync = require("redline.sync")
    if sync.is_open() then
      sync.close()
    else
      require("redline.quit").quit()
    end
  end, "close the sync output split, else the whole redline session (asks)" },
  { "n", "P", M.push, "push to Overleaf (pull+merge, then push)" },
  { "n", "L", M.pull, "pull from Overleaf" },
  { "n", "W", function() require("redline.layout").pick() end, "pane layout preset" },
  { "n", "f", function() core.config.follow = not core.config.follow; vim.notify("redline: PDF follow " .. (core.config.follow and "on" or "off")) end, "toggle PDF follow" },
  { "n", "?", function() M.help() end, "help" },
  { "n", "q", function() M.exit() end, "leave review mode" },
  { "n", "<Esc>", function() M.exit() end, "leave review mode" },
  { "n", "i", function() M.exit(); vim.cmd("startinsert") end, "leave review mode and insert" },
}

function M.help()
  local lines = { " Redline review mode", "" }
  for _, k in ipairs(REVIEW_KEYS) do
    table.insert(lines, ("  %-2s %-6s %s"):format(k[1] == "x" and "v" or "", k[2], k[4]))
  end
  table.insert(lines, "")
  table.insert(lines, "  u / <C-r> undo/redo as usual; markers: %Y accept, %N keep, %X reject")
  table.insert(lines, "  % VERIFY <method>: «claim»  ->  % VERIFIED | REFUTED | UNVERIFIED")
  local b = vim.api.nvim_create_buf(false, true)
  vim.api.nvim_buf_set_lines(b, 0, -1, false, lines)
  local w = vim.api.nvim_open_win(b, true, { relative = "editor", row = 2, col = 4, width = 64,
    height = #lines, border = "rounded", style = "minimal" })
  vim.keymap.set("n", "q", "<cmd>close<CR>", { buffer = b })
  vim.keymap.set("n", "<Esc>", "<cmd>close<CR>", { buffer = b })
  vim.keymap.set("n", "?", "<cmd>close<CR>", { buffer = b })
  return w
end

local saved = {} -- buf -> {maps, wo}

function M.enter()
  local buf, st = cur()
  if not st then
    st = core.attach(buf)
  end
  if st.review then
    return
  end
  st.review = true
  local prev = {}
  for _, k in ipairs(REVIEW_KEYS) do
    local m = vim.fn.maparg(k[2], k[1], false, true)
    if m and m.buffer == 1 then
      table.insert(prev, m)
    end
    vim.keymap.set(k[1], k[2], k[3], { buffer = buf, nowait = true, desc = "redline: " .. k[4] })
  end
  saved[buf] = { maps = prev, cl = vim.wo.conceallevel, cc = vim.wo.concealcursor }
  vim.wo.conceallevel = 2
  vim.wo.concealcursor = "n"
  st.follow_au = vim.api.nvim_create_autocmd("CursorHold", {
    buffer = buf,
    callback = function()
      if core.config.follow then
        preview.show_cursor(st.root)
      end
    end,
  })
  core.refresh(buf)
  require("redline.statusline").update()
  vim.notify("redline: review mode (? for keys, q to leave)")
end

function M.exit()
  local buf, st = cur()
  if not (st and st.review) then
    return
  end
  st.review = false
  for _, k in ipairs(REVIEW_KEYS) do
    pcall(vim.keymap.del, k[1], k[2], { buffer = buf })
  end
  local sv = saved[buf] or {}
  for _, m in ipairs(sv.maps or {}) do
    vim.fn.mapset(m.mode, false, m)
  end
  if sv.cl then
    vim.wo.conceallevel = sv.cl
    vim.wo.concealcursor = sv.cc
  end
  if st.follow_au then
    pcall(vim.api.nvim_del_autocmd, st.follow_au)
  end
  core.refresh(buf)
  require("redline.statusline").update()
end

function M.toggle()
  local _, st = cur()
  if st and st.review then
    M.exit()
  else
    M.enter()
  end
end

--- Always-on buffer maps (outside review mode).
function M.buffer_maps(buf)
  local p = core.config.prefix
  local map = function(mode, lhs, rhs, desc, opts)
    opts = vim.tbl_extend("force", { buffer = buf, desc = "redline: " .. desc }, opts or {})
    vim.keymap.set(mode, lhs, rhs, opts)
  end
  map("n", core.config.review_key, M.toggle, "toggle review mode")
  map("n", "]r", function() M.next() end, "next change")
  map("n", "[r", function() M.prev() end, "previous change")
  map("n", p .. "a", function() return M.operator("accept") end, "accept {motion}", { expr = true })
  map("n", p .. "x", function() return M.operator("reject") end, "reject {motion}", { expr = true })
  map("n", p .. "A", function() M.resolve_current("accept") end, "accept change under cursor")
  map("n", p .. "X", function() M.resolve_current("reject") end, "reject change under cursor")
  map("x", p .. "a", function() M.resolve_range("accept", true) end, "accept selection")
  map("x", p .. "x", function() M.resolve_range("reject", true) end, "reject selection")
  map("n", p .. "n", function() M.mark_current("N") end, "keep pending with a note")
  map("n", p .. "d", M.discuss, "ask the agent")
  map("n", p .. "N", M.send_notes, "send all %N notes to the agent")
  map("x", p .. "d", M.send_visual, "ask the agent about the selection")
  map("n", p .. "l", function() require("redline.picker").pick() end, "list changes")
  map("n", p .. "c", function() M.compile() end, "compile")
  map("n", p .. "F", M.fix_with_agent, "send compile errors to the agent")
  map("n", p .. "r", function()
    vim.wo.conceallevel = vim.wo.conceallevel >= 2 and 0 or 2
  end, "toggle concealed/raw markup")
  map("n", p .. "p", function() preview.show_cursor(core.state().root, true) end, "show in PDF")
  map("n", p .. "s", function() require("redline.verify").request(false) end, "verify claim on this line")
  map("x", p .. "s", function() require("redline.verify").request(true) end, "verify selected claim")
  map("n", p .. "S", function() require("redline.verify").send_all() end, "send open verify requests")
  map("n", p .. "v", function() require("redline.verify").evidence() end, "evidence window (toggle)")
  map("n", p .. "o", function() require("redline.sync").sync() end, "sync to Overleaf")
  map("n", p .. "q", function() require("redline.sync").close() end, "close the sync output split")
  map("n", p .. "Q", function() require("redline.quit").quit() end, "close the whole redline session (asks)")
  map("n", p .. "w", function() require("redline.layout").pick() end, "pane layout preset")
  map("n", p .. "W", function() require("redline.layout").next() end, "next pane layout")
end

return M
