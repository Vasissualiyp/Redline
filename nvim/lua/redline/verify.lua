-- Ask the agent to verify a claim; the request lives in the paper as
-- `% VERIFY <method>: «claim»` and is answered with `% VERIFIED ...`.
local core = require("redline")
local rpc = require("redline.rpc")
local tmux = require("redline.tmux")

local M = {}

local CHOICES = {
  { method = "refcheck", send = true, label = "refcheck: literature (Zotero PDFs) — ask the agent now" },
  { method = "code", send = true, label = "code: analysis repo — ask the agent now" },
  { method = "any", send = true, label = "either — ask the agent now" },
  { method = "refcheck", send = false, label = "refcheck — queue only (send all later with S)" },
  { method = "code", send = false, label = "code — queue only" },
}

local function selection()
  local mode = vim.fn.mode()
  local p1, p2 = vim.fn.getpos("v"), vim.fn.getpos(".")
  local lines = vim.fn.getregion(p1, p2, { type = mode })
  vim.api.nvim_feedkeys(vim.api.nvim_replace_termcodes("<Esc>", true, false, true), "nx", false)
  return math.min(p1[2], p2[2]), math.max(p1[2], p2[2]), table.concat(lines, " ")
end

--- Send the open requests (all, or the given ids) to the agent pane.
function M.send(ids)
  local st = core.state()
  if not st then
    return
  end
  local res, err = rpc.call(st.root, "verify_message", { text = core.buf_text(0), ids = ids })
  if err then
    return vim.notify("redline: " .. err, vim.log.levels.WARN)
  end
  tmux.send_agent(res.message)
end

--- The result under the cursor, or the one directly below the claim line the cursor is on.
local function result_at(row)
  local st = core.state()
  local by_line = {}
  for _, r in ipairs(st and st.data and st.data.results or {}) do
    by_line[r.line] = r
  end
  if by_line[row] then
    return by_line[row]
  end
  local n, last = row + 1, vim.api.nvim_buf_line_count(0)
  while n <= last do
    if by_line[n] then
      return by_line[n]
    end
    local text = vim.api.nvim_buf_get_lines(0, n - 1, n, false)[1] or ""
    if not text:match("^%s*%%") then
      return nil -- only verify comment lines sit between a claim and its result
    end
    n = n + 1
  end
end

--- Open (or close) the evidence window for the result at the cursor.
function M.evidence()
  local st = core.state()
  if not st then
    return vim.notify("redline: buffer not attached")
  end
  local r = result_at(vim.api.nvim_win_get_cursor(0)[1])
  if not r then
    return vim.notify("redline: no % VERIFIED/REFUTED/UNVERIFIED line here")
  end
  if r.id == vim.NIL or not r.id then
    return vim.notify("redline: this result carries its evidence inline; `redline evidence migrate` "
      .. "moves it into the library", vim.log.levels.WARN)
  end
  vim.system(rpc.shell("evidence show " .. vim.fn.shellescape(r.id)),
    { cwd = st.root, text = true, env = { REDLINE_ROOT = st.root } },
    vim.schedule_wrap(function(res)
      local msg = vim.trim((res.stdout or "") .. (res.stderr or ""))
      if res.code ~= 0 then
        vim.notify("redline: " .. msg, vim.log.levels.WARN)
      elseif msg ~= "" then
        vim.notify("redline: " .. msg)
      end
    end))
end

function M.request(visual)
  local st = core.state() or core.attach()
  local first, last, claim
  if visual then
    first, last, claim = selection()
  else
    first = vim.fn.line(".")
    last = first
    claim = vim.api.nvim_get_current_line()
  end
  claim = vim.trim(claim:gsub("%s*%%.*$", ""))
  vim.ui.select(CHOICES, {
    prompt = ("Verify lines %d-%d"):format(first, last),
    format_item = function(c) return c.label end,
  }, function(choice)
    if not choice then
      return
    end
    local res, err = rpc.call(st.root, "add_verify", {
      text = core.buf_text(0), after_line = last, method = choice.method, claim = claim,
    })
    if err then
      return vim.notify("redline: " .. err, vim.log.levels.ERROR)
    end
    core.apply_edits(0, res.edits)
    core.refresh(0)
    if choice.send and res.id then
      M.send({ res.id })
    else
      vim.notify("redline: verification request queued")
    end
  end)
end

function M.send_all()
  local st = core.state()
  local n = st and st.data and #(st.data.verify or {}) or 0
  if n == 0 then
    return vim.notify("redline: no open % VERIFY requests")
  end
  if vim.fn.confirm(("Send %d open verification request(s) to the agent?"):format(n), "&Yes\n&No", 1) == 1 then
    M.send(nil)
  end
end

return M
