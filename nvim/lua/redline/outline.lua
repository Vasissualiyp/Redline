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
-- keys (? inside): j k move; <CR> to the paper; h l za zM zR fold; J K move in the plan; x cut,
-- p P gp paste; u U undo / reset the plan; A apply the plan; m mark, M merge, S split; r R thoughts
-- (model / agent pane); f flow check; ]f [f next flag; s sidebar <-> tab; q close.
local core = require("redline")
local rpc = require("redline.rpc")

local M = {}
local S = {
  buf = nil, win = nil, mode = "side", root = nil, paper = nil, data = nil, plan = nil, hist = {},
  collapsed = {}, cut = nil, marks = {}, touched = {}, width = nil, wide = false, lines = {}, busy = nil, timer = nil, follow_timer = nil,
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
    if not present[n.hash] then
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
  return PRE_FMT:format(fmt(n.nlines or 0), fmt(n.words or 0), fmt(n.chars or 0))
end

local function render()
  if not (S.buf and vim.api.nvim_buf_is_valid(S.buf)) then return end
  local list = nodes()
  local moved, nmoved = moved_set()
  local vis = visible()
  local lines, marks = {}, {}
  S.lines = {}
  local d = S.data or { total = 0, stale = 0, missing = 0, flags = 0 }
  local head = (" Overview  %d blocks · %d stale · %d without thought · %d flags"):format(d.total, d.stale, d.missing, d.flags)
  if S.plan then head = head .. ("  · PLAN: %d moved"):format(nmoved or 0) end
  if S.busy then head = head .. "  · " .. S.busy end
  table.insert(lines, head)
  table.insert(lines, PRE_FMT:format("l", "w", "c") .. "? keys · r thoughts · f flow · J K x p plan · A apply")
  for _, v in ipairs(vis) do
    local n = v.n
    local pad = ("  "):rep(math.max(n.depth, 0))
    local fold = " "
    if has_children(v.i, list) then fold = S.collapsed[n.hash] and "▸" or "▾" end
    local text, group
    if n.kind == "heading" then
      text = n.title
      group = "RedlineOutlineHead"
    elseif n.thought then
      text = n.thought
      group = n.stale and "RedlineOutlineStale" or HL[n.kind]
    else
      text = "∅ " .. (n.text or ""):sub(1, 90)
      group = "RedlineOutlineMissing"
    end
    local tag = (n.stale and "~" or "") .. (S.plan and S.touched[n.hash] and "⇅" or "") .. (S.marks[n.hash] and "◆" or "")
      .. (S.cut == n.hash and "✂" or "")
    local line = columns(n) .. ("%s%s%s%s %s"):format(pad, fold, ICON[n.kind] or "?", tag ~= "" and tag or "", text)
    table.insert(lines, line)
    local row = #lines - 1
    S.lines[row] = v
    table.insert(marks, { row, 0, PRE, "RedlineOutlineCounts" })
    local icon_from = PRE + #pad + #fold
    table.insert(marks, { row, icon_from, #line, S.cut == n.hash and "RedlineOutlineCut" or group })
    if (S.plan and S.touched[n.hash]) or S.marks[n.hash] then
      table.insert(marks, { row, icon_from, icon_from + #ICON[n.kind] + #tag, S.marks[n.hash] and "RedlineOutlineMark" or "RedlineOutlineMoved" })
    end
    for _, f in ipairs(n.flags or {}) do
      local label = FLAG_ICON[f.kind == "repeat" and "repeat_" or f.kind] or "⚑"
      table.insert(marks, { row, -1, -1, nil, { virt = (" "):rep(PRE - 2) .. ("%s    %s: %s"):format(pad, label, f.note) } })
    end
  end
  vim.bo[S.buf].modifiable = true
  vim.api.nvim_buf_set_lines(S.buf, 0, -1, false, lines)
  vim.bo[S.buf].modifiable = false
  vim.api.nvim_buf_clear_namespace(S.buf, ns, 0, -1)
  vim.api.nvim_buf_set_extmark(S.buf, ns, 0, 0, { end_col = #lines[1], hl_group = "RedlineOutlineHeader" })
  vim.api.nvim_buf_set_extmark(S.buf, ns, 1, 0, { end_col = #lines[2], hl_group = "RedlineOutlineCounts" })
  vim.api.nvim_buf_set_extmark(S.buf, ns, 1, 0, { end_col = PRE, hl_group = "RedlineOutlineHeader" })
  local virt = {}
  for _, m in ipairs(marks) do
    local extra = m[5]
    if extra and extra.virt then
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
  local text = n.kind == "heading" and n.title or (n.text or ""):sub(1, 1500)
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
  end)
end

local function busy(label)
  S.busy = label
  if S.buf and vim.api.nvim_buf_is_valid(S.buf) then redraw_keep() end
end

-- ---- the plan ----------------------------------------------------------------------
local function plan_begin()
  if not S.plan then
    S.plan = vim.deepcopy(S.data.nodes)
  end
  table.insert(S.hist, vim.deepcopy(S.plan))
  if #S.hist > 50 then table.remove(S.hist, 1) end
end

local function subtree(list, i)
  local n, j = list[i], i
  while list[j + 1] and list[j + 1].depth > n.depth and (n.kind == "heading" or n.kind == "list") do j = j + 1 end
  return i, j
end

local function move_sibling(step)
  local v = cur_vis()
  if not v then return end
  plan_begin()
  local list = S.plan
  local i = v.i
  local a, b = subtree(list, i)
  local n = list[i]
  local ia, ib
  if step > 0 then
    local nx = list[b + 1]
    if not nx or nx.depth ~= n.depth or (nx.kind == "heading") ~= (n.kind == "heading") and n.kind == "heading" then
      table.remove(S.hist); return vim.notify("redline overview: nothing below to swap with (use x / p to move across sections)") end
    local _, nb = subtree(list, b + 1)
    local chunk = {}
    for k = a, b do table.insert(chunk, list[k]) end
    for _ = a, b do table.remove(list, a) end
    for k, x in ipairs(chunk) do table.insert(list, nb - (b - a) + k - 0, x) end
  else
    local k = a - 1
    while k >= 1 and list[k].depth > n.depth do k = k - 1 end
    local pv = list[k]
    if not pv or pv.depth ~= n.depth then
      table.remove(S.hist); return vim.notify("redline overview: nothing above to swap with (use x / p to move across sections)") end
    local chunk = {}
    for q = a, b do table.insert(chunk, list[q]) end
    for _ = a, b do table.remove(list, a) end
    for q, x in ipairs(chunk) do table.insert(list, k + q - 1, x) end
  end
  S.touched[n.hash] = true
  recompute_depths(S.plan)
  redraw_keep()
  cursor_to_hash(n.hash)
end

local function cut()
  local v = cur_vis()
  if not v then return end
  S.cut = v.n.hash
  render()
  vim.notify("redline overview: cut «" .. ((v.n.thought or v.n.title or v.n.text or ""):sub(1, 50)) .. "»; p / P / gp paste")
end

local function paste(where)
  if not S.cut then return vim.notify("redline overview: nothing cut (x)") end
  local v = cur_vis()
  if not v then return end
  plan_begin()
  local list = S.plan
  local ci
  for i, n in ipairs(list) do if n.hash == S.cut then ci = i end end
  if not ci or list[ci].hash == v.n.hash then table.remove(S.hist); return end
  local a, b = subtree(list, ci)
  local chunk = {}
  for k = a, b do table.insert(chunk, list[k]) end
  for _ = a, b do table.remove(list, a) end
  local ti
  for i, n in ipairs(list) do if n.hash == v.n.hash then ti = i end end
  if not ti then S.plan = table.remove(S.hist); return end
  local at
  if where == "before" then at = ti
  elseif where == "into" then at = ti + 1
  else local _, tb = subtree(list, ti); at = tb + 1 end
  for k, x in ipairs(chunk) do table.insert(list, at + k - 1, x) end
  S.cut = nil
  S.touched[chunk[1].hash] = true
  recompute_depths(S.plan)
  redraw_keep()
  cursor_to_hash(chunk[1].hash)
end

local function plan_undo()
  local prev = table.remove(S.hist)
  if not prev then return vim.notify("redline overview: nothing to undo") end
  S.plan = prev
  if #S.hist == 0 then S.plan, S.touched = nil, {} end -- back to the paper's own order
  redraw_keep()
end

local function plan_reset()
  S.plan, S.hist, S.cut, S.touched = nil, {}, nil, {}
  redraw_keep()
  vim.notify("redline overview: plan discarded")
end

-- ---- the agent -------------------------------------------------------------------
local function send(text)
  require("redline.tmux").send_agent(text)
end

local function apply_plan()
  if not S.plan then return vim.notify("redline overview: no plan (J K x p rearrange first)") end
  local _, n = moved_set()
  if n == 0 then return vim.notify("redline overview: the plan changes nothing") end
  if vim.fn.confirm(("Hand %d move(s) to the agent as tracked moves?"):format(n), "&Yes\n&No", 1) ~= 1 then return end
  local order = {}
  for _, x in ipairs(S.plan) do table.insert(order, x.hash) end
  run("overview plan-set", { stdin = vim.json.encode(order) }, function(r1)
    if r1.code ~= 0 then return vim.notify("redline overview: " .. (r1.stderr or ""), vim.log.levels.ERROR) end
    run("overview plan-message", nil, function(r2)
      if r2.code ~= 0 then return vim.notify("redline overview: " .. (r2.stderr or ""), vim.log.levels.ERROR) end
      send(r2.stdout)
      vim.notify("redline overview: plan sent to the agent; refresh (gr) when it is done")
    end)
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
      { name = "Move", hl = "Move", title = "Move and fold", entries = {
        { key = "j k", mode = "n", desc = "move (the paper and the PDF follow: the whole block is highlighted); columns l w c = lines, words, chars" },
        { key = "<CR> o", mode = "n", desc = "go to the block in the paper" },
        { key = "h l za", mode = "n", desc = "fold: h closes / goes to the parent, l opens, za toggles" },
        { key = "zM zR", mode = "n", desc = "close all sections / open all" },
        { key = "]f [f", mode = "n", desc = "next / previous flow flag" } } },
      { name = "Decide", hl = "Decide", title = "Rearrange (a draft plan; the paper is untouched)", entries = {
        { key = "J K", mode = "n", desc = "move the block (or section with its content) down / up among its siblings" },
        { key = "x", mode = "n", desc = "cut the block / section" },
        { key = "p P gp", mode = "n", desc = "paste after / before / into the section under the cursor" },
        { key = "u U", mode = "n", desc = "undo one plan step / discard the whole plan" },
        { key = "A", mode = "n", desc = "apply the plan: the agent executes it as tracked moves" } } },
      { name = "Agent", hl = "Agent", title = "Merge and split with the agent", entries = {
        { key = "m", mode = "n", desc = "mark / unmark the block for merging" },
        { key = "M", mode = "n", desc = "merge the marked blocks (or this one and the next) into one paragraph" },
        { key = "S", mode = "n", desc = "split this block into several thoughts" } } },
      { name = "Build", hl = "Build", title = "Thoughts and flow", entries = {
        { key = "r", mode = "n", desc = "write missing / stale thoughts with the outline_model (cheap model, headless)" },
        { key = "R", mode = "n", desc = "same, through the paper's agent pane" },
        { key = "W", mode = "n", desc = "rewrite EVERY thought (e.g. after changing outline_words)" },
        { key = "f", mode = "n", desc = "flow check: the model flags gaps, repeats, forward references ..." },
        { key = "gr", mode = "n", desc = "reload (also automatic after you edit the paper)" } } },
      { name = "Session", hl = "Session", title = "Window", entries = {
        { key = "s", mode = "n", desc = "sidebar <-> full tab" },
        { key = "> <", mode = "n", desc = "wider / narrower sidebar (also <C-w>< <C-w>> or drag the border with the mouse)" },
        { key = "|", mode = "n", desc = "toggle a wide sidebar (60% of the screen) and back" },
        { key = "q", mode = "n", desc = "close" } } },
    },
    notes = { "~ stale thought (the block changed)   ∅ no thought yet   ⇅ moved in the plan   ◆ marked   ✂ cut" },
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

local function setup_buffer()
  S.buf = vim.api.nvim_create_buf(false, true)
  vim.bo[S.buf].bufhidden = "hide"
  vim.bo[S.buf].filetype = "redline-outline"
  vim.bo[S.buf].modifiable = false
  vim.api.nvim_buf_set_name(S.buf, "redline://overview")
  map("<CR>", function() local v = cur_vis() if v then show_node(by_hash()[v.n.hash] or v.n, true) end end, "go to the paper")
  map("o", function() local v = cur_vis() if v then show_node(by_hash()[v.n.hash] or v.n, true) end end, "go to the paper")
  map("l", function() fold(true) end, "open")
  map("h", function() fold(false) end, "close / parent")
  map("za", function() fold(nil) end, "toggle fold")
  map("zM", function() fold_all(true) end, "close all")
  map("zR", function() fold_all(false) end, "open all")
  map("J", function() move_sibling(1) end, "move down")
  map("K", function() move_sibling(-1) end, "move up")
  map("x", cut, "cut")
  map("p", function() paste("after") end, "paste after")
  map("P", function() paste("before") end, "paste before")
  map("gp", function() paste("into") end, "paste into the section")
  map("u", plan_undo, "undo")
  map("U", plan_reset, "discard the plan")
  map("A", apply_plan, "apply the plan")
  map("m", function()
    local v = cur_vis()
    if v and v.n.kind ~= "heading" then S.marks[v.n.hash] = (not S.marks[v.n.hash]) or nil; redraw_keep() end
  end, "mark")
  map("M", function() edit_task("merge") end, "merge")
  map("S", function() edit_task("split") end, "split")
  map("r", function() model_task("refresh") end, "thoughts")
  map("R", function() model_task("refresh", "pane") end, "thoughts via the agent pane")
  map("W", function()
    if vim.fn.confirm("Rewrite EVERY thought (the current ones are dropped)?", "&Yes\n&No", 2) == 1 then model_task("refresh", nil, true) end
  end, "rewrite every thought")
  map("f", function() model_task("flow") end, "flow check")
  map("]f", function() next_flag(1) end, "next flag")
  map("[f", function() next_flag(-1) end, "previous flag")
  map("gr", function() fetch() end, "reload")
  map("s", function() M.toggle_mode() end, "sidebar <-> tab")
  map(">", function() resize(6) end, "wider")
  map("<", function() resize(-6) end, "narrower")
  map("|", toggle_wide, "wide <-> normal")
  map("q", function() M.close() end, "close")
  map("?", help, "help")
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
  for k, v in pairs({ wrap = true, linebreak = true, breakindent = true, number = false, relativenumber = false,
    cursorline = true, signcolumn = "no", foldcolumn = "0", winfixwidth = S.mode == "side", list = false }) do
    vim.wo[S.win][k] = v
  end
  vim.wo[S.win].showbreak = (" "):rep(PRE - 4) .. "│ " -- wrapped thoughts stay under the thought column
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
