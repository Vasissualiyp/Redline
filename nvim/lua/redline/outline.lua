-- High-level overview (`gO`): the paper as a tree of one-line "thoughts".
--
-- One node per paragraph, figure, table, list (and its items) and equation, under the section
-- headings; each shows its thought with char/word/line counts, dimmed and marked ~ when the
-- block changed since the thought was written. Moving the cursor shows the block in the paper
-- buffer and highlights the WHOLE block in the PDF pane. You can rearrange the thoughts as a
-- draft plan (the paper is untouched) and then hand the plan to the agent as tracked moves,
-- or ask the agent to merge / split blocks. Thoughts are written by a cheap model of your
-- choice (outline_model in .redline.toml); the flow check adds coloured flags.
--
-- The buffer is editable, like Oil: dd a line (or a visual block, or a folded section) and p it elsewhere
-- to MOVE it, dd alone to DELETE it, then :w to commit the draft plan (the paper is untouched until
-- gA hands the plan to the agent as tracked moves / deletions). Numbers and relative numbers are on, so
-- 50dd, 12j, :m work as usual. Unwanted edits: gr reloads. ? lists the keys.
local core = require("redline")
local rpc = require("redline.rpc")

local M = {}
local S = {
  buf = nil, win = nil, mode = "side", root = nil, paper = nil, data = nil, plan = nil,
  collapsed = {}, marks = {}, deleted = {}, width = nil, wide = false, hide_counts = nil, lines = {}, busy = nil, timer = nil, follow_timer = nil,
}
M._state = S -- for tests
local ns = vim.api.nvim_create_namespace("redline_outline")
local ns_paper = vim.api.nvim_create_namespace("redline_outline_paper")
local FIRST = 2 -- header lines before the first node

local ICON = { heading = "§", paragraph = "¶", abstract = "A", figure = "▣", table = "▦", list = "≡", item = "•",
  equation = "∑", env = "◇" }
local HL = {
  heading = "RedlineOutlineHead", paragraph = "RedlineOutlinePara", abstract = "RedlineOutlineAbstract",
  figure = "RedlineOutlineFigure", table = "RedlineOutlineTable", list = "RedlineOutlineList",
  item = "RedlineOutlineList", equation = "RedlineOutlineMath", env = "RedlineOutlinePara",
}
local FLAG_ICON = { gap = "⚠ gap", repeat_ = "⟲ repeat", ["forward-ref"] = "↰ forward reference",
  ["weak-transition"] = "∼ weak transition", ["orphan-figure"] = "▣ orphan figure", other = "⚑" }

local function define_highlights()
  local function hl(name, spec) spec.default = true; vim.api.nvim_set_hl(0, name, spec) end
  hl("RedlineOutlineHead", { fg = "#61afef", bold = true })
  hl("RedlineOutlinePara", { fg = "#abb2bf" })
  hl("RedlineOutlineAbstract", { fg = "#98c379", bold = true })
  hl("RedlineOutlineFigure", { fg = "#56b6c2" })
  hl("RedlineOutlineTable", { fg = "#2bbc8a" })
  hl("RedlineOutlineList", { fg = "#d19a66" })
  hl("RedlineOutlineMath", { fg = "#c678dd" })
  hl("RedlineOutlineStale", { fg = "#7f848e", italic = true })
  hl("RedlineOutlineMissing", { fg = "#5c6370", italic = true })
  hl("RedlineOutlineCounts", { fg = "#5c6370" })
  hl("RedlineOutlineMoved", { fg = "#e5c07b", bold = true })
  hl("RedlineOutlineCut", { fg = "#5c6370", strikethrough = true })
  hl("RedlineOutlineMark", { fg = "#e06c75", bold = true })
  hl("RedlineOutlineFlag", { fg = "#e06c75", italic = true })
  hl("RedlineOutlineHeader", { fg = "#abb2bf", bold = true, underline = true })
  hl("RedlineOutlineBlock", { bg = "#3e4451" })
  hl("RedlineOutlineHere", { bg = "#2c313c", bold = true }) -- the block the paper's cursor is in
end

-- ---- data --------------------------------------------------------------------------
local function root_and_buf()
  local st = core.state()
  if st then
    return st.root, vim.api.nvim_get_current_buf()
  end
end

local function run(args, opts, cb)
  opts = opts or {}
  vim.system(rpc.shell(args), { cwd = S.root, text = true, stdin = opts.stdin, env = { REDLINE_ROOT = S.root } },
    vim.schedule_wrap(function(r) cb(r) end))
end

local function nodes() -- what is shown: the plan when there is one, else the paper's order
  return S.plan or (S.data and S.data.nodes) or {}
end

local function by_hash()
  local t = {}
  for _, n in ipairs(S.data and S.data.nodes or {}) do t[n.hash] = n end
  return t
end

local function recompute_depths(list)
  -- blocks belong to the closest heading before them; items to their list (depth + 1)
  local stack, lastlist = {}, nil
  for _, n in ipairs(list) do
    if n.kind == "heading" then
      while #stack > 0 and stack[#stack].level >= n.level do table.remove(stack) end
      n.depth = #stack
      table.insert(stack, n)
      lastlist = nil
    elseif n.kind == "item" and lastlist then
      n.depth = lastlist.depth + 1
    else
      n.depth = #stack
      lastlist = n.kind == "list" and n or nil
    end
  end
end

local function reconcile_plan()
  -- the paper changed under a plan: keep the plan's order for what still exists, put new blocks after their predecessor
  if not S.plan or not S.data then return end
  local fresh, present = by_hash(), {}
  local out = {}
  for _, n in ipairs(S.plan) do
    if fresh[n.hash] then
      table.insert(out, vim.tbl_extend("force", fresh[n.hash], { depth = n.depth }))
      present[n.hash] = true
    end
  end
  local prev
  for _, n in ipairs(S.data.nodes) do
    if not present[n.hash] and not S.deleted[n.hash] then
      local at = #out
      for i, m in ipairs(out) do if m.hash == (prev or "") then at = i break end end
      table.insert(out, at + 1, vim.deepcopy(n))
    end
    prev = n.hash
  end
  S.plan = out
  recompute_depths(S.plan)
end

local function moved_set()
  -- nodes outside the longest common subsequence of the paper's order and the plan's
  if not S.plan then return {} end
  local a, b = {}, {}
  for _, n in ipairs(S.data.nodes) do table.insert(a, n.hash) end
  for _, n in ipairs(S.plan) do table.insert(b, n.hash) end
  local A, B = #a, #b
  local dp = {}
  for i = 0, A + 1 do dp[i] = {} for j = 0, B + 1 do dp[i][j] = 0 end end
  for i = A, 1, -1 do
    for j = B, 1, -1 do
      dp[i][j] = a[i] == b[j] and dp[i + 1][j + 1] + 1 or math.max(dp[i + 1][j], dp[i][j + 1])
    end
  end
  local keep, i, j = {}, 1, 1
  while i <= A and j <= B do
    if a[i] == b[j] then keep[a[i]] = true; i = i + 1; j = j + 1
    elseif dp[i + 1][j] >= dp[i][j + 1] then i = i + 1 else j = j + 1 end
  end
  local moved, n = {}, 0
  for _, h in ipairs(b) do if not keep[h] then moved[h] = true; n = n + 1 end end
  return moved, n
end

-- ---- drawing -----------------------------------------------------------------------
local function visible()
  local out, skip_depth = {}, nil
  for i, n in ipairs(nodes()) do
    if skip_depth and n.depth > skip_depth then
      -- inside a collapsed node
    else
      skip_depth = nil
      table.insert(out, { i = i, n = n })
      if S.collapsed[n.hash] and (n.kind == "heading" or n.kind == "list") then skip_depth = n.depth end
    end
  end
  return out
end

local function has_children(i, list)
  local n, nx = list[i], list[i + 1]
  return nx ~= nil and nx.depth > n.depth and (n.kind == "heading" or n.kind == "list")
end

local function fmt(n) return tostring(n):reverse():gsub("(%d%d%d)", "%1,"):reverse():gsub("^,", "") end

-- fixed columns before the thought: lines, words, chars (right-aligned, so sizes line up)
local PRE_FMT = "%4s %5s %7s │ "
local PRE = #(PRE_FMT:format("", "", "")) -- bytes; "│" is 3 bytes
local function columns(n)
  if S.hide_counts then return "" end
  return PRE_FMT:format(fmt(n.nlines or 0), fmt(n.words or 0), fmt(n.chars or 0))
end
local function pre() return S.hide_counts and 0 or PRE end -- bytes of the l/w/c columns right now
local function pre_width() return S.hide_counts and 0 or PRE - 2 end -- screen columns ("│" is 1 wide, 3 bytes)



local function render()
  if not (S.buf and vim.api.nvim_buf_is_valid(S.buf)) then return end
  local list = nodes()
  local moved = moved_set()
  local nmoved = 0
  for _ in pairs(moved) do nmoved = nmoved + 1 end
  local ndel = 0
  for _ in pairs(S.deleted) do ndel = ndel + 1 end
  local vis = visible()
  local lines, marks = {}, {}
  S.lines = {}
  local d = S.data or { total = 0, stale = 0, missing = 0, flags = 0 }
  local head = (" Overview  %d blocks · %d stale · %d without thought · %d flags"):format(d.total, d.stale, d.missing, d.flags)
  if S.plan then head = head .. ("  · PLAN: %d moved, %d deleted (gA applies, gr discards)"):format(nmoved, ndel) end
  if S.busy then head = head .. "  · " .. S.busy end
  table.insert(lines, head)
  table.insert(lines, (S.hide_counts and "" or PRE_FMT:format("l", "w", "c")) .. "? keys · dd p move · dd delete · :w commit · gA apply plan · gc l/w/c")
  for _, v in ipairs(vis) do
    local n = v.n
    local pad = ("  "):rep(math.max(n.depth, 0))
    local fold = " "
    if has_children(v.i, list) then fold = S.collapsed[n.hash] and "▸" or "▾" end
    local text, group
    if n.kind == "heading" then
      text = ((n.number or "") ~= "" and (n.number .. "  ") or "") .. n.title -- 3.4.1  Title
      group = "RedlineOutlineHead"
    elseif n.thought then
      text = n.thought
      group = n.stale and "RedlineOutlineStale" or HL[n.kind]
    else
      text = "∅ " .. (n.text or ""):sub(1, 90)
      group = "RedlineOutlineMissing"
    end
    local tag = (n.stale and "~" or "") .. (S.plan and moved[n.hash] and "⇅" or "") .. (S.marks[n.hash] and "◆" or "")
    local id = "‹" .. n.hash .. "›"
    -- the id goes LAST: concealed text still counts when nvim wraps, and 16 hidden columns at the
    -- start of every line made all of them wrap that much too early
    local body = columns(n) .. ("%s%s%s%s %s"):format(pad, fold, ICON[n.kind] or "?", tag ~= "" and tag or "", text)
    local line = body .. id
    table.insert(lines, line)
    local row = #lines - 1
    S.lines[row] = v
    table.insert(marks, { row, #body, #line, nil, { conceal = true } })
    table.insert(marks, { row, 0, pre(), "RedlineOutlineCounts" })
    local icon_from = pre() + #pad + #fold
    table.insert(marks, { row, icon_from, #body, group })
    if (S.plan and moved[n.hash]) or S.marks[n.hash] then
      table.insert(marks, { row, icon_from, icon_from + #ICON[n.kind] + #tag, S.marks[n.hash] and "RedlineOutlineMark" or "RedlineOutlineMoved" })
    end
    for _, f in ipairs(n.flags or {}) do
      local label = FLAG_ICON[f.kind == "repeat" and "repeat_" or f.kind] or "⚑"
      table.insert(marks, { row, -1, -1, nil, { virt = (" "):rep(pre_width()) .. ("%s    %s: %s"):format(pad, label, f.note) } })
    end
  end
  S.rendering = true
  vim.api.nvim_buf_set_lines(S.buf, 0, -1, false, lines)
  vim.bo[S.buf].modified = false
  S.rendering = false
  vim.api.nvim_buf_clear_namespace(S.buf, ns, 0, -1)
  vim.api.nvim_buf_set_extmark(S.buf, ns, 0, 0, { end_col = #lines[1], hl_group = "RedlineOutlineHeader" })
  vim.api.nvim_buf_set_extmark(S.buf, ns, 1, 0, { end_col = #lines[2], hl_group = "RedlineOutlineCounts" })
  if pre() > 0 then
    vim.api.nvim_buf_set_extmark(S.buf, ns, 1, 0, { end_col = pre(), hl_group = "RedlineOutlineHeader" })
  end
  local virt = {}
  for _, m in ipairs(marks) do
    local extra = m[5]
    if extra and extra.conceal then
      vim.api.nvim_buf_set_extmark(S.buf, ns, m[1], m[2], { end_col = m[3], conceal = "" })
    elseif extra and extra.virt then
      virt[m[1]] = virt[m[1]] or {}
      table.insert(virt[m[1]], { { extra.virt, "RedlineOutlineFlag" } })
    elseif m[4] then
      vim.api.nvim_buf_set_extmark(S.buf, ns, m[1], m[2], { end_col = m[3], hl_group = m[4] })
    end
  end
  for row, vl in pairs(virt) do
    vim.api.nvim_buf_set_extmark(S.buf, ns, row, 0, { virt_lines = vl })
  end
end

local function cur_vis()
  if not (S.win and vim.api.nvim_win_is_valid(S.win)) then return nil end
  local row = vim.api.nvim_win_get_cursor(S.win)[1] - 1
  return S.lines[row]
end

local function cursor_to_hash(h)
  for row, v in pairs(S.lines) do
    if v.n.hash == h and S.win and vim.api.nvim_win_is_valid(S.win) then
      pcall(vim.api.nvim_win_set_cursor, S.win, { row + 1, 0 })
      return
    end
  end
end

local function redraw_keep()
  local v = cur_vis()
  local h = v and v.n.hash
  render()
  if h then cursor_to_hash(h) end
end

-- ---- following the cursor: paper buffer + PDF highlight --------------------------------
local function paper_win()
  if not (S.paper and vim.api.nvim_buf_is_valid(S.paper)) then return nil end
  local w = vim.fn.bufwinid(S.paper)
  return w ~= -1 and w or nil
end

local function show_node(n, focus)
  if not n then return end
  local a, b = n.lines[1], n.lines[2]
  local w = paper_win()
  vim.api.nvim_buf_clear_namespace(S.paper, ns_paper, 0, -1)
  if S.paper and vim.api.nvim_buf_is_valid(S.paper) then
    local last = vim.api.nvim_buf_line_count(S.paper)
    for l = a, math.min(b, last) do
      vim.api.nvim_buf_set_extmark(S.paper, ns_paper, l - 1, 0, { line_hl_group = "RedlineOutlineBlock" })
    end
  end
  if w then
    vim.api.nvim_win_set_cursor(w, { math.min(a, vim.api.nvim_buf_line_count(S.paper)), 0 })
    vim.api.nvim_win_call(w, function() vim.cmd("normal! zz") end)
    if focus then vim.api.nvim_set_current_win(w) end
  end
  local text = n.kind == "heading" and n.title or (n.text or ""):sub(1, 40000) -- the whole block: a cap here is where the PDF highlight would stop
  require("redline.preview").show(S.root, a, true, text, b)
end

local function follow()
  if S.follow_timer then S.follow_timer:stop() end
  S.follow_timer = S.follow_timer or vim.uv.new_timer()
  S.follow_timer:start(120, 0, vim.schedule_wrap(function()
    local v = cur_vis()
    if v and vim.api.nvim_get_current_win() == S.win then show_node(by_hash()[v.n.hash] or v.n, false) end
  end))
end

-- ---- the other direction: the overview follows the paper's cursor ---------------------------
local ns_here = vim.api.nvim_create_namespace("redline_outline_here")

--- The node a paper line belongs to: the smallest block containing it, else the nearest one above.
local function node_at_line(line)
  local best, best_span, above, above_start = nil, math.huge, nil, -1
  for _, n in ipairs(S.data and S.data.nodes or {}) do
    local a, b = n.lines[1], n.lines[2]
    if a <= line and line <= b and (b - a) < best_span then best, best_span = n, b - a end
    if a <= line and a > above_start then above, above_start = n, a end
  end
  return best or above
end

local function sync_from_paper()
  if not (M.is_open() and S.paper and vim.api.nvim_buf_is_valid(S.paper)) then return end
  if vim.api.nvim_get_current_win() == S.win or vim.api.nvim_get_current_buf() ~= S.paper then return end
  local line = vim.api.nvim_win_get_cursor(0)[1]
  local node = node_at_line(line)
  if not node then return end
  -- inside a folded section: the nearest visible ancestor stands for it (the fold is the user's choice)
  local nodes_ = S.data.nodes
  local target = node
  while target and not (function()
    for _, v in pairs(S.lines) do if v.n.hash == target.hash then return true end end
  end)() do
    target = target.parent and target.parent >= 0 and nodes_[target.parent + 1] or nil
  end
  if not target then return end
  for row, v in pairs(S.lines) do
    if v.n.hash == target.hash then
      vim.api.nvim_buf_clear_namespace(S.buf, ns_here, 0, -1)
      vim.api.nvim_buf_set_extmark(S.buf, ns_here, row, 0, { line_hl_group = "RedlineOutlineHere" })
      pcall(vim.api.nvim_win_set_cursor, S.win, { row + 1, 0 })
      vim.api.nvim_win_call(S.win, function() vim.cmd("normal! zz") end)
      return
    end
  end
end

-- ---- loading ---------------------------------------------------------------------
local function fetch(cb)
  run("overview --json", nil, function(r)
    local ok, data = pcall(vim.json.decode, r.stdout or "", { luanil = { object = true, array = true } })
    if not ok or type(data) ~= "table" then
      return vim.notify("redline overview: " .. ((r.stderr or "") .. (r.stdout or "")):sub(1, 300), vim.log.levels.ERROR)
    end
    S.data = data
    if S.plan then reconcile_plan() end
    redraw_keep()
    if cb then cb() end
    vim.defer_fn(sync_from_paper, 20)
  end)
end

local function busy(label)
  S.busy = label
  if S.buf and vim.api.nvim_buf_is_valid(S.buf) then redraw_keep() end
end

-- ---- the plan: edit the buffer, :w commits it --------------------------------------
local function hidden_below(list, i)
  -- the nodes folded away under a collapsed heading / list: they travel with it (or go with it)
  local out, n = {}, list[i]
  for j = i + 1, #list do
    if list[j].depth <= n.depth then break end
    table.insert(out, list[j])
  end
  return out
end

--- Read the buffer: its lines' hidden ids give the wanted order; ids that are gone are deleted.
local function commit()
  if not (S.buf and vim.api.nvim_buf_is_valid(S.buf) and S.data) then return end
  local fresh = by_hash()
  local before = nodes()
  local at = {}
  for i, n in ipairs(before) do at[n.hash] = i end
  local order, seen, dup = {}, {}, 0
  for _, line in ipairs(vim.api.nvim_buf_get_lines(S.buf, 0, -1, false)) do
    local h = line:match("‹(%x+)›")
    if h and fresh[h] then
      if seen[h] then
        dup = dup + 1 -- a yanked copy: copies are not supported, the first one counts
      else
        seen[h] = true
        local node = fresh[h]
        table.insert(order, vim.tbl_extend("force", node, { depth = node.depth }))
        if S.collapsed[h] and at[h] then -- a folded section moves (or goes) as a whole
          for _, sub in ipairs(hidden_below(before, at[h])) do
            if not seen[sub.hash] then
              seen[sub.hash] = true
              table.insert(order, vim.tbl_extend("force", fresh[sub.hash] or sub, {}))
            end
          end
        end
      end
    end
  end
  S.deleted = {}
  for _, n in ipairs(S.data.nodes) do
    if not seen[n.hash] then S.deleted[n.hash] = true end
  end
  recompute_depths(order)
  local same = #order == #S.data.nodes
  if same then for i, n in ipairs(order) do if n.hash ~= S.data.nodes[i].hash then same = false break end end end
  S.plan = (not same or next(S.deleted)) and order or nil
  redraw_keep()
  local _, nm = moved_set()
  local nd = 0
  for _ in pairs(S.deleted) do nd = nd + 1 end
  vim.notify(S.plan and ("redline overview: plan has %d moved, %d deleted block(s)%s; gA applies it, gr discards it")
    :format(nm or 0, nd, dup > 0 and ("; " .. dup .. " copy line(s) ignored") or "") or "redline overview: no changes to the paper's order")
end

local function plan_reset()
  S.plan, S.deleted = nil, {}
  fetch()
  vim.notify("redline overview: plan discarded, reloaded from the paper")
end

-- ---- the agent -------------------------------------------------------------------
local function send(text)
  require("redline.tmux").send_agent(text)
end

local function plan_counts()
  local _, n = moved_set()
  local nd = 0
  for _ in pairs(S.deleted) do nd = nd + 1 end
  return n or 0, nd
end

local function plan_payload()
  local order, deleted = {}, {}
  for _, x in ipairs(S.plan) do table.insert(order, x.hash) end
  for h in pairs(S.deleted) do table.insert(deleted, h) end
  return vim.json.encode({ order = order, deleted = deleted })
end

local function apply_plan()
  if vim.bo[S.buf].modified then commit() end
  if not S.plan then return vim.notify("redline overview: no plan (dd / p lines, then :w)") end
  local n, nd = plan_counts()
  if n == 0 and nd == 0 then return vim.notify("redline overview: the plan changes nothing") end
  local q = ("Apply the plan: %d move(s), %d deletion(s)%s"):format(n, nd, nd > 0 and "  (deletions are tracked, you can reject them)" or "")
  -- directly = declaratively, no agent: every move / deletion becomes tracked markup in one undoable write
  local pick = vim.fn.confirm(q, "&Directly (tracked, no agent)\n&Hand to the agent\n&Cancel", 1)
  if pick ~= 1 and pick ~= 2 then return end
  run("overview plan-set", { stdin = plan_payload() }, function(r1)
    if r1.code ~= 0 then return vim.notify("redline overview: " .. (r1.stderr or ""), vim.log.levels.ERROR) end
    if pick == 1 then
      core.flush()
      run("overview plan-apply", nil, function(r2)
        local out = vim.trim((r2.stdout or "") .. (r2.stderr or ""))
        vim.notify("redline overview: " .. out, r2.code == 0 and vim.log.levels.INFO or vim.log.levels.WARN)
        if r2.code == 0 then
          S.plan, S.deleted = nil, {}
          core.reload()
          fetch()
        end
      end)
    else
      run("overview plan-message", nil, function(r2)
        if r2.code ~= 0 then return vim.notify("redline overview: " .. (r2.stderr or ""), vim.log.levels.ERROR) end
        send(r2.stdout)
        vim.notify("redline overview: plan sent to the agent; gr reloads when it is done")
      end)
    end
  end)
end

local function edit_task(op)
  local hashes = {}
  if op == "merge" then
    for h in pairs(S.marks) do table.insert(hashes, h) end
    if #hashes == 1 then -- one mark: merge it with the block that follows
      local list = S.data.nodes
      for i, n in ipairs(list) do
        if n.hash == hashes[1] and list[i + 1] and list[i + 1].kind ~= "heading" then
          table.insert(hashes, list[i + 1].hash)
        end
      end
    end
    if #hashes == 0 then
      local v = cur_vis()
      local list = nodes()
      local nx = v and list[v.i + 1]
      if not (v and nx and nx.kind ~= "heading" and v.n.kind ~= "heading") then
        return vim.notify("redline overview: mark the blocks to merge with m (or put the cursor on the first of two neighbours)")
      end
      hashes = { v.n.hash, nx.hash }
    else
      local want, order = {}, {}
      for _, h in ipairs(hashes) do want[h] = true end
      for _, n in ipairs(S.data.nodes) do if want[n.hash] then table.insert(order, n.hash) end end -- document order
      hashes = order
    end
  else
    local v = cur_vis()
    if not v or v.n.kind == "heading" then return vim.notify("redline overview: put the cursor on the block to split") end
    hashes = { v.n.hash }
  end
  vim.ui.input({ prompt = (op == "merge" and ("Merge %d thoughts: "):format(#hashes) or "Split this thought: ")
    .. "(what should come out? empty = your call) " }, function(instr)
    if instr == nil then return end
    run(("overview edit-message --op %s --hashes %s --instruction %s"):format(op, table.concat(hashes, ","),
      vim.fn.shellescape(instr)), nil, function(r)
      if r.code ~= 0 then return vim.notify("redline overview: " .. (r.stderr or ""), vim.log.levels.ERROR) end
      send(r.stdout)
      S.marks = {}
      redraw_keep()
      vim.notify("redline overview: " .. op .. " request sent to the agent")
    end)
  end)
end

local function model_task(action, model, everything)
  if S.busy then return vim.notify("redline overview: busy (" .. S.busy .. ")") end
  local label = action == "flow" and "checking the flow" or "writing thoughts"
  busy("⠿ " .. label)
  local stop = require("redline.spinner").start(label)
  run("overview " .. action .. (model and (" --model " .. model) or "") .. (everything and " --all" or ""), nil, function(r)
    stop()
    busy(nil)
    local out = ((r.stdout or "") .. (r.stderr or "")):gsub("%s+$", "")
    vim.notify("redline overview: " .. (out:match("[^\n]+$") or "done"), r.code == 0 and vim.log.levels.INFO or vim.log.levels.WARN)
    fetch()
  end)
end

local function next_flag(step)
  local list = nodes()
  local v = cur_vis()
  local i = v and v.i or 0
  for _ = 1, #list do
    i = (i - 1 + step) % #list + 1
    if list[i].flags and #list[i].flags > 0 then
      S.collapsed = {}
      render()
      cursor_to_hash(list[i].hash)
      return
    end
  end
  vim.notify("redline overview: no flow flags (f runs the check)")
end

-- ---- folds ---------------------------------------------------------------------------
local function fold(set)
  local v = cur_vis()
  if not v then return end
  if not has_children(v.i, nodes()) then
    if set == false then -- h on a leaf: up to the parent
      local list = nodes()
      for k = v.i - 1, 1, -1 do
        if list[k].depth < v.n.depth then cursor_to_hash(list[k].hash) return end
      end
    end
    return
  end
  if set == nil then S.collapsed[v.n.hash] = not S.collapsed[v.n.hash]
  elseif set == false and S.collapsed[v.n.hash] then
    local list = nodes()
    for k = v.i - 1, 1, -1 do if list[k].depth < v.n.depth then cursor_to_hash(list[k].hash) return end end
  else S.collapsed[v.n.hash] = (set == false) or nil end
  redraw_keep()
end

local function fold_all(collapse)
  S.collapsed = {}
  if collapse then
    for _, n in ipairs(nodes()) do if n.kind == "heading" and n.depth == 0 then S.collapsed[n.hash] = true end end
  end
  render()
end

-- ---- the window ---------------------------------------------------------------------
local function help()
  require("redline.help").open({
    groups = {
      { name = "Decide", hl = "Decide", title = "Rearrange like Oil: edit the lines, :w commits a draft plan", entries = {
        { key = "dd  p P", mode = "n", desc = "cut a line (or 50dd, V...d, a folded section) and paste it elsewhere = MOVE it" },
        { key = "dd", mode = "n", desc = "cut without pasting = DELETE the block (asked again before it reaches the agent)" },
        { key = ":w", mode = "n", desc = "commit: parse the lines into the plan (moved ⇅, deleted counted in the header)" },
        { key = "u  <C-r>", mode = "n", desc = "undo / redo the edits as usual" },
        { key = "gA", mode = "n", desc = "apply the plan: directly (declarative tracked moves / deletions, one undo) or via the agent" },
        { key = "gr", mode = "n", desc = "discard the plan / any edit and reload from the paper" } } },
      { name = "Move", hl = "Move", title = "Move and fold (numbers and relative numbers are on)", entries = {
        { key = "j k 12j gg G H M L <C-d>", mode = "n", desc = "all the usual motions" },
        { key = "<CR>", mode = "n", desc = "go to the block in the paper (the PDF highlights the whole block while you move)" },
        { key = "za zo zc zM zR <Tab>", mode = "n", desc = "fold / unfold; a folded section moves or goes as a whole" },
        { key = "]f [f", mode = "n", desc = "next / previous flow flag" } } },
      { name = "Agent", hl = "Agent", title = "Merge and split with the agent", entries = {
        { key = "m", mode = "n", desc = "mark / unmark the block for merging" },
        { key = "gm", mode = "n", desc = "merge the marked blocks (or this one and the next) into one paragraph" },
        { key = "gs", mode = "n", desc = "split this block into several thoughts" } } },
      { name = "Build", hl = "Build", title = "Thoughts and flow", entries = {
        { key = "gt", mode = "n", desc = "write missing / stale thoughts with the outline_model (cheap model, headless)" },
        { key = "gT", mode = "n", desc = "same, through the paper's agent pane" },
        { key = "gW", mode = "n", desc = "rewrite EVERY thought (e.g. after changing outline_words)" },
        { key = "gF", mode = "n", desc = "flow check: the model flags gaps, repeats, forward references ..." } } },
      { name = "Session", hl = "Session", title = "Window", entries = {
        { key = "gc", mode = "n", desc = "show / hide the l w c (lines, words, chars) columns" },
        { key = "gw", mode = "n", desc = "sidebar <-> full tab" },
        { key = "g> g< g|", mode = "n", desc = "wider / narrower sidebar / toggle a wide one (also <C-w>< <C-w>> or the mouse)" },
        { key = "q", mode = "n", desc = "close (asks if the lines were edited but not committed)" } } },
    },
    notes = { "~ stale thought   ∅ no thought yet   ⇅ moved in the plan   ◆ marked.  The end of every line holds a hidden id (‹...›): do not edit it." },
  })
end

local function resize(delta)
  if not M.is_open() or S.mode ~= "side" then return end
  local w = vim.api.nvim_win_get_width(S.win) + delta
  w = math.max(24, math.min(vim.o.columns - 20, w))
  vim.api.nvim_win_set_width(S.win, w)
  S.width, S.wide = w, false
end

local function toggle_wide()
  if not M.is_open() or S.mode ~= "side" then return end
  if S.wide then
    vim.api.nvim_win_set_width(S.win, S.normal_width or 52)
    S.width, S.wide = S.normal_width or 52, false
  else
    S.normal_width = vim.api.nvim_win_get_width(S.win)
    vim.api.nvim_win_set_width(S.win, math.floor(vim.o.columns * 0.6))
    S.wide = true
  end
end

local function map(lhs, fn, desc) vim.keymap.set("n", lhs, fn, { buffer = S.buf, nowait = true, silent = true, desc = "redline overview: " .. desc }) end

local function goto_current()
  local v = cur_vis()
  if v then show_node(by_hash()[v.n.hash] or v.n, true) end
end

local function setup_buffer()
  S.buf = vim.api.nvim_create_buf(false, true)
  vim.bo[S.buf].bufhidden = "hide"
  vim.bo[S.buf].buftype = "acwrite" -- :w is ours: it commits the plan
  vim.bo[S.buf].filetype = "redline-outline"
  vim.bo[S.buf].swapfile = false
  vim.api.nvim_buf_set_name(S.buf, "redline://overview")
  map("<CR>", goto_current, "go to the paper")
  map("<Tab>", function() fold(nil) end, "toggle fold")
  map("zo", function() fold(true) end, "open")
  map("zc", function() fold(false) end, "close")
  map("za", function() fold(nil) end, "toggle fold")
  map("zM", function() fold_all(true) end, "close all")
  map("zR", function() fold_all(false) end, "open all")
  map("gA", apply_plan, "apply the plan")
  map("gr", function() plan_reset() end, "discard the plan and reload")
  map("m", function()
    local v = cur_vis()
    if v and v.n.kind ~= "heading" then S.marks[v.n.hash] = (not S.marks[v.n.hash]) or nil; redraw_keep() end
  end, "mark")
  map("gm", function() edit_task("merge") end, "merge")
  map("gs", function() edit_task("split") end, "split")
  map("gt", function() model_task("refresh") end, "thoughts")
  map("gT", function() model_task("refresh", "pane") end, "thoughts via the agent pane")
  map("gW", function()
    if vim.fn.confirm("Rewrite EVERY thought (the current ones are dropped)?", "&Yes\n&No", 2) == 1 then model_task("refresh", nil, true) end
  end, "rewrite every thought")
  map("gF", function() model_task("flow") end, "flow check")
  map("]f", function() next_flag(1) end, "next flag")
  map("[f", function() next_flag(-1) end, "previous flag")
  map("gc", function()
    S.hide_counts = not S.hide_counts or nil
    M.set_showbreak()
    redraw_keep()
  end, "show / hide the l w c columns")
  map("gw", function() M.toggle_mode() end, "sidebar <-> tab")
  map("g>", function() resize(6) end, "wider")
  map("g<", function() resize(-6) end, "narrower")
  map("g|", toggle_wide, "wide <-> normal")
  map("q", function()
    if vim.bo[S.buf].modified and vim.fn.confirm("Lines were edited but not committed (:w). Close anyway?", "&Yes\n&No", 2) ~= 1 then return end
    vim.bo[S.buf].modified = false
    M.close()
  end, "close")
  map("?", help, "help")
  vim.api.nvim_create_autocmd("BufWriteCmd", { buffer = S.buf, callback = function()
    commit()
    vim.bo[S.buf].modified = false
  end })
  vim.api.nvim_create_autocmd("CursorMoved", { buffer = S.buf, callback = follow })
  vim.api.nvim_create_autocmd("WinResized", { callback = function()  -- a manual resize (mouse, <C-w><) is kept
    if S.mode == "side" and M.is_open() and vim.tbl_contains(vim.v.event.windows or {}, S.win) and not S.wide then
      S.width = vim.api.nvim_win_get_width(S.win)
    end
  end })
end

local function open_window()
  if S.mode == "tab" then
    vim.cmd("tab split")
  else
    vim.cmd(("topleft %dvsplit"):format(S.width or core.config.outline_width or 52))
  end
  S.win = vim.api.nvim_get_current_win()
  vim.api.nvim_win_set_buf(S.win, S.buf)
  for k, v in pairs({ wrap = true, linebreak = true, breakindent = true, number = true, relativenumber = true, numberwidth = 3,
    conceallevel = 3, concealcursor = "nvic",
    cursorline = true, signcolumn = "no", foldcolumn = "0", winfixwidth = S.mode == "side", list = false }) do
    vim.wo[S.win][k] = v
  end
  M.set_showbreak()
end

function M.set_showbreak()
  if M.is_open() then -- wrapped thoughts stay under the thought column
    vim.wo[S.win].showbreak = S.hide_counts and "" or ((" "):rep(PRE - 4) .. "│ ")
  end
end

function M.is_open() return S.win ~= nil and vim.api.nvim_win_is_valid(S.win) end

function M.close()
  if S.paper and vim.api.nvim_buf_is_valid(S.paper) then vim.api.nvim_buf_clear_namespace(S.paper, ns_paper, 0, -1) end
  if M.is_open() then pcall(vim.api.nvim_win_close, S.win, true) end
  S.win = nil
end

function M.open(mode)
  define_highlights()
  local root, paper = root_and_buf()
  if not root then
    if S.root then root = S.root else return vim.notify("redline: open the paper first") end
  else
    S.root, S.paper = root, paper
  end
  if mode then S.mode = mode end
  if not (S.buf and vim.api.nvim_buf_is_valid(S.buf)) then setup_buffer() end
  if not M.is_open() then open_window() else vim.api.nvim_set_current_win(S.win) end
  S.data = S.data or { nodes = {}, total = 0, stale = 0, missing = 0, flags = 0 }
  render()
  fetch(function()
    if not M.is_open() then return end
    local v = S.data and S.data.nodes
    if v and #v > 0 and vim.api.nvim_win_get_cursor(S.win)[1] <= FIRST then pcall(vim.api.nvim_win_set_cursor, S.win, { FIRST + 1, 0 }) end
    if S.data.missing + S.data.stale > 0 then
      vim.notify(("redline overview: %d block(s) have no up-to-date thought; r writes them with %s"):format(S.data.missing + S.data.stale, "the outline_model"))
    end
  end)
  -- the paper changed: reload (debounced) so staleness shows up while you write
  S.au = S.au or vim.api.nvim_create_augroup("RedlineOutline", { clear = true })
  vim.api.nvim_clear_autocmds({ group = S.au, buffer = S.paper })
  -- the overview moves with the paper: the block under the paper's cursor is shown and highlighted
  vim.api.nvim_create_autocmd("CursorMoved", { group = S.au, buffer = S.paper, callback = function()
    S.paper_timer = S.paper_timer or vim.uv.new_timer()
    S.paper_timer:stop()
    S.paper_timer:start(80, 0, vim.schedule_wrap(sync_from_paper))
  end })
  vim.api.nvim_create_autocmd({ "BufWritePost", "TextChanged" }, { group = S.au, buffer = S.paper, callback = function()
    if not M.is_open() then return end
    S.timer = S.timer or vim.uv.new_timer()
    S.timer:stop()
    S.timer:start(1500, 0, vim.schedule_wrap(function() if M.is_open() then fetch() end end))
  end })
end

function M.toggle()
  if M.is_open() then M.close() else M.open("side") end
end

function M.toggle_mode()
  local mode = S.mode == "side" and "tab" or "side"
  M.close()
  M.open(mode)
end

return M
