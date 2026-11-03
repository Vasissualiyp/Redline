-- Choose models: the agent pane (claude / opencode models) and, on a second tab, the model that
-- writes the overview's thoughts and runs its flow check (outline_model).
--   <Tab> / h l   switch tab        j k   move        <CR>   choose        q <Esc>   close
local core = require("redline")
local rpc = require("redline.rpc")

local M = {}

local TABS = {
  { key = "agent", title = "Agent pane", hint = "the interactive agent next to the paper (restarts it)" },
  { key = "outline", title = "Overview model", hint = "writes the thoughts and runs the flow check (gO)" },
}

local function run(args, root, cb)
  vim.system(rpc.shell(args), { cwd = root, text = true, env = { REDLINE_ROOT = root } }, vim.schedule_wrap(cb))
end

local function load(root, which, cb)
  run("agent --list --json" .. (which == "outline" and " --for outline" or ""), root, function(r)
    local ok, items = pcall(vim.json.decode, r.stdout or "")
    if not ok or type(items) ~= "table" then
      return vim.notify("redline agent: " .. vim.trim((r.stderr or "") .. (r.stdout or "")), vim.log.levels.ERROR)
    end
    cb(items)
  end)
end

local function choose(root, which, it, done)
  if it.problem ~= "" then
    return vim.notify("redline: " .. it.label .. ": " .. it.problem, vim.log.levels.WARN)
  end
  if which == "agent" then
    local q = (it.current and "Restart " or "Switch the agent pane to ") .. it.label .. "? The conversation in it ends."
    if vim.fn.confirm(q, "&Yes\n&No", 1) ~= 1 then
      return
    end
  end
  run("agent " .. vim.fn.shellescape(it.name) .. (which == "outline" and " --for outline" or ""), root, function(res)
    local msg = vim.trim((res.stdout or "") .. (res.stderr or ""))
    vim.notify("redline: " .. msg, res.code == 0 and vim.log.levels.INFO or vim.log.levels.WARN)
    if done then done(res.code == 0) end
  end)
end

--- tab: "agent" (default) or "outline"
function M.pick(tab)
  local st = core.state()
  if not st then
    return vim.notify("redline: buffer not attached")
  end
  vim.api.nvim_set_hl(0, "RedlineAgentTab", { fg = "#e5c07b", bold = true, underline = true, default = true })
  vim.api.nvim_set_hl(0, "RedlineAgentTabOff", { fg = "#7f848e", default = true })
  vim.api.nvim_set_hl(0, "RedlineAgentNow", { fg = "#98c379", bold = true, default = true })
  vim.api.nvim_set_hl(0, "RedlineAgentProblem", { fg = "#e06c75", italic = true, default = true })
  vim.notify("redline: checking the models…")
  local data, which = {}, tab == "outline" and 2 or 1
  local buf = vim.api.nvim_create_buf(false, true)
  vim.bo[buf].bufhidden = "wipe"
  local ns = vim.api.nvim_create_namespace("redline_agent_pick")
  local win

  local function render()
    local items = data[TABS[which].key] or {}
    local head = " "
    local marks = {}
    for i, t in ipairs(TABS) do
      local from = #head
      head = head .. t.title .. "   "
      table.insert(marks, { from, #head - 3, i == which and "RedlineAgentTab" or "RedlineAgentTabOff" })
    end
    local lines = { head, " " .. TABS[which].hint, "" }
    for _, it in ipairs(items) do
      table.insert(lines, ("  %s %s%s"):format(it.current and "●" or " ", it.label,
        it.problem ~= "" and ("   [" .. it.problem .. "]") or ""))
    end
    if #items == 0 then table.insert(lines, "  loading…") end
    vim.bo[buf].modifiable = true
    vim.api.nvim_buf_set_lines(buf, 0, -1, false, lines)
    vim.bo[buf].modifiable = false
    vim.api.nvim_buf_clear_namespace(buf, ns, 0, -1)
    for _, m in ipairs(marks) do
      vim.api.nvim_buf_set_extmark(buf, ns, 0, m[1], { end_col = m[2], hl_group = m[3] })
    end
    vim.api.nvim_buf_set_extmark(buf, ns, 1, 0, { end_col = #lines[2], hl_group = "RedlineAgentTabOff" })
    for i, it in ipairs(items) do
      local row = 2 + i
      if it.current then
        vim.api.nvim_buf_set_extmark(buf, ns, row, 0, { end_col = #lines[row + 1], hl_group = "RedlineAgentNow" })
      elseif it.problem ~= "" then
        vim.api.nvim_buf_set_extmark(buf, ns, row, 0, { end_col = #lines[row + 1], hl_group = "RedlineAgentProblem" })
      end
    end
    if vim.api.nvim_win_is_valid(win) then
      local cur = 1
      for i, it in ipairs(items) do if it.current then cur = i end end
      pcall(vim.api.nvim_win_set_cursor, win, { math.min(3 + cur, #lines), 0 })
    end
  end

  local function ensure(which_i)
    local key = TABS[which_i].key
    if data[key] then return end
    load(st.root, key, function(items)
      data[key] = items
      if which == which_i and vim.api.nvim_buf_is_valid(buf) then render() end
    end)
  end

  local width = math.min(86, vim.o.columns - 6)
  win = vim.api.nvim_open_win(buf, true, { relative = "editor", row = 3, col = math.floor((vim.o.columns - width) / 2),
    width = width, height = 14, border = "rounded", style = "minimal", title = " Models ", title_pos = "center",
    footer = " <Tab> tab   j/k   <CR> choose   q close ", footer_pos = "center" })
  vim.wo[win].cursorline = true
  render()
  ensure(which)

  local function switch(step)
    which = (which - 1 + step) % #TABS + 1
    ensure(which)
    render()
  end
  local function map(lhs, fn) vim.keymap.set("n", lhs, fn, { buffer = buf, nowait = true }) end
  map("<Tab>", function() switch(1) end)
  map("<S-Tab>", function() switch(-1) end)
  map("l", function() switch(1) end)
  map("h", function() switch(-1) end)
  map("q", function() vim.api.nvim_win_close(win, true) end)
  map("<Esc>", function() vim.api.nvim_win_close(win, true) end)
  map("<CR>", function()
    local items = data[TABS[which].key] or {}
    local it = items[vim.api.nvim_win_get_cursor(win)[1] - 3]
    if not it then return end
    local key = TABS[which].key
    vim.api.nvim_win_close(win, true)
    choose(st.root, key, it)
  end)
end

return M
