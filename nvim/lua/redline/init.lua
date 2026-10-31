-- redline.nvim: review `changes`-package suggestions with vim keys.
local rpc = require("redline.rpc")
local render = require("redline.render")

local M = {}

M.config = {
  prefix = "<leader>j", -- operator prefix outside review mode
  review_key = "<leader>R", -- toggle review mode
  autosave = true, -- keep the file on disk in sync so agents see your edits
  autosave_delay = 800,
  follow = true, -- move the PDF pane with the cursor in review mode
  labels = true, -- show C12 (and %N notes) at the end of changed lines in review mode
  label_origin = false, -- also show who made the change (claude-code, opencode, ...)
  auto_compile = 10, -- compile once the paper has been quiet this many seconds (0 = off)
}

-- per-buffer state: { root, data, tick, review, saved_text, watcher }
M.bufs = {}

local function find_root(path)
  if vim.env.REDLINE_ROOT and vim.env.REDLINE_ROOT ~= "" then
    local r = vim.fs.normalize(vim.env.REDLINE_ROOT)
    if vim.startswith(vim.fs.normalize(path), r) then
      return r
    end
  end
  local found = vim.fs.find(".redline.toml", { upward = true, path = vim.fs.dirname(path) })[1]
  return found and vim.fs.dirname(found) or vim.fs.dirname(path)
end

function M.buf_text(buf)
  local lines = vim.api.nvim_buf_get_lines(buf, 0, -1, false)
  local text = table.concat(lines, "\n")
  if vim.bo[buf].eol or vim.bo[buf].fixeol then
    text = text .. "\n"
  end
  return text
end

function M.state(buf)
  return M.bufs[buf or vim.api.nvim_get_current_buf()]
end

-- ---- rendering -------------------------------------------------------------
function M.refresh(buf, cb)
  buf = buf or vim.api.nvim_get_current_buf()
  local st = M.bufs[buf]
  if not st then
    return
  end
  local tick = vim.api.nvim_buf_get_changedtick(buf)
  rpc.request(st.root, "parse", { text = M.buf_text(buf) }, function(res, err)
    if err then
      vim.notify("redline: " .. err, vim.log.levels.WARN)
      return
    end
    if not vim.api.nvim_buf_is_valid(buf) or vim.api.nvim_buf_get_changedtick(buf) ~= tick then
      return -- stale; a newer refresh is on its way
    end
    st.data = res
    render.draw(buf, res, { conceal = st.review, labels = st.review and M.config.labels,
      label_origin = M.config.label_origin })
    if cb then
      cb(res)
    end
  end)
end

local function debounce(fn, ms)
  local timer = vim.uv.new_timer()
  return function(...)
    local args = { ... }
    timer:stop()
    timer:start(ms, 0, vim.schedule_wrap(function()
      fn(unpack(args))
    end))
  end
end

--- Apply rpc edits ({range = {sr, sc, er, ec}, text}) as one undo step.
function M.apply_edits(buf, edits)
  if not edits or #edits == 0 then
    return false
  end
  local first = true
  for _, e in ipairs(edits) do -- already sorted last-to-first
    local r = e.range
    local lines = vim.split(e.text, "\n", { plain = true })
    if not first then
      pcall(vim.cmd, "undojoin")
    end
    vim.api.nvim_buf_set_text(buf, r[1], r[2], r[3], r[4], lines)
    first = false
  end
  return true
end

-- ---- file sync with agents --------------------------------------------------
--- Called by redline (CLI/MCP) through `nvim --server` before it edits the file.
local function save(buf)
  if not (vim.api.nvim_buf_is_valid(buf) and vim.bo[buf].modified) then
    return
  end
  vim.api.nvim_buf_call(buf, function()
    vim.cmd("silent! checktime") -- an outside edit is merged in first (FileChangedShell)
    if vim.bo[buf].modified and M.bufs[buf] and not M.bufs[buf].merging then
      vim.cmd("silent! lockmarks update")
    end
  end)
end

function M.flush()
  for buf, _ in pairs(M.bufs) do
    save(buf)
  end
  return "ok"
end

--- Called after redline wrote the file.
function M.reload()
  vim.schedule(function()
    for buf, _ in pairs(M.bufs) do
      if vim.api.nvim_buf_is_valid(buf) then
        vim.api.nvim_buf_call(buf, function()
          vim.cmd("silent! checktime")
        end)
      end
    end
  end)
  return "ok"
end

local function merge3(ours, base, theirs)
  local dir = vim.fn.tempname()
  vim.fn.mkdir(dir, "p")
  local function put(name, text)
    local f = io.open(dir .. "/" .. name, "w")
    f:write(text)
    f:close()
    return dir .. "/" .. name
  end
  local o, b, t = put("ours", ours), put("base", base), put("theirs", theirs)
  local r = vim.system({ "git", "merge-file", "-p", "-L", "yours", "-L", "base", "-L", "disk", o, b, t },
    { text = true }):wait()
  vim.fn.delete(dir, "rf")
  return r.stdout, r.code
end

local function on_file_changed(buf)
  local st = M.bufs[buf]
  if not st then
    return
  end
  if not vim.bo[buf].modified then
    vim.v.fcs_choice = "reload"
    return
  end
  -- both changed: merge the disk version into the buffer
  vim.v.fcs_choice = ""
  st.merging = true
  local path = vim.api.nvim_buf_get_name(buf)
  local f = io.open(path, "r")
  if not f then
    return
  end
  local disk = f:read("*a")
  f:close()
  vim.schedule(function()
    st.merging = false
    local merged, conflicts = merge3(M.buf_text(buf), st.saved_text or disk, disk)
    if merged then
      local lines = vim.split(merged:gsub("\n$", ""), "\n", { plain = true })
      vim.api.nvim_buf_set_lines(buf, 0, -1, false, lines)
      vim.api.nvim_buf_call(buf, function()
        vim.cmd("silent! lockmarks write!") -- the disk version is merged in already
      end)
      if conflicts > 0 then
        vim.notify(("redline: merged an outside edit with %d conflict(s); search <<<<<<<"):format(conflicts),
          vim.log.levels.WARN)
      end
    end
  end)
end

local function remember_saved(buf)
  local st = M.bufs[buf]
  if st then
    st.saved_text = M.buf_text(buf)
  end
end

local function watch(buf)
  local st = M.bufs[buf]
  local path = vim.api.nvim_buf_get_name(buf)
  if st.watcher then
    st.watcher:stop()
  end
  local w = vim.uv.new_fs_event()
  st.watcher = w
  w:start(path, {}, vim.schedule_wrap(function(err, _, events)
    if err or not vim.api.nvim_buf_is_valid(buf) then
      return
    end
    vim.api.nvim_buf_call(buf, function()
      vim.cmd("silent! checktime")
    end)
    if events and events.rename then
      watch(buf) -- the file was replaced; watch the new inode
    end
  end))
end

-- ---- status -------------------------------------------------------------------
function M.item_at(buf, row, col)
  local st = M.bufs[buf]
  if not (st and st.data) then
    return nil
  end
  for i, it in ipairs(st.data.items) do
    local r = it.full_range
    local after_start = row > r[1] or (row == r[1] and col >= r[2])
    local before_end = row < r[3] or (row == r[3] and col < r[4])
    if after_start and before_end then
      return it, i
    end
  end
  return nil
end

function M.status()
  local buf = vim.api.nvim_get_current_buf()
  local st = M.bufs[buf]
  if not (st and st.data) then
    return ""
  end
  local pos = vim.api.nvim_win_get_cursor(0)
  local it = M.item_at(buf, pos[1] - 1, pos[2])
  local n, marked = #st.data.items, 0
  for _, x in ipairs(st.data.items) do
    if x.marker then
      marked = marked + 1
    end
  end
  local s = (it and (it.label .. "/" .. n) or (n .. " pending"))
  if it and it.section ~= "" then
    s = s .. " · §" .. it.section
  end
  if marked > 0 then
    s = s .. " · " .. marked .. " marked"
  end
  if st.review then
    s = "REVIEW " .. s
  end
  return s
end

-- ---- attaching ------------------------------------------------------------------
function M.attach(buf)
  buf = buf or vim.api.nvim_get_current_buf()
  if M.bufs[buf] then
    return M.bufs[buf]
  end
  local path = vim.api.nvim_buf_get_name(buf)
  local st = { root = find_root(path), review = false }
  M.bufs[buf] = st
  render.define_highlights()
  vim.bo[buf].autoread = true
  remember_saved(buf)

  local group = vim.api.nvim_create_augroup("redline_" .. buf, { clear = true })
  local refresh = debounce(function()
    M.refresh(buf)
  end, 250)
  vim.api.nvim_create_autocmd({ "TextChanged", "TextChangedI", "BufReadPost" },
    { group = group, buffer = buf, callback = function() refresh() end })
  vim.api.nvim_create_autocmd("ColorScheme", { group = group, callback = render.define_highlights })
  vim.api.nvim_create_autocmd("FileChangedShell", {
    group = group, buffer = buf, callback = function() on_file_changed(buf) end })
  vim.api.nvim_create_autocmd({ "BufWritePost", "BufReadPost" }, {
    group = group, buffer = buf, callback = function() remember_saved(buf) end })
  vim.api.nvim_create_autocmd("BufWipeout", {
    group = group, buffer = buf, callback = function()
      if st.watcher then
        st.watcher:stop()
      end
      M.bufs[buf] = nil
    end })
  if M.config.autosave then
    local autosave = debounce(function()
      if vim.fn.mode() ~= "i" then
        save(buf)
      end
    end, M.config.autosave_delay)
    vim.api.nvim_create_autocmd({ "TextChanged", "InsertLeave" },
      { group = group, buffer = buf, callback = function() autosave() end })
  end
  vim.api.nvim_create_autocmd({ "TextChanged", "InsertLeave" },
    { group = group, buffer = buf, callback = function() require("redline.autocompile").poke(buf) end })
  watch(buf)
  require("redline.review").buffer_maps(buf)
  M.refresh(buf)
  return st
end

--- Cursor to file:line (the PDF pane's `e`), in the window showing that file or the paper's.
function M.goto_source(file, line)
  local target = vim.fs.normalize(file)
  local win
  for _, w in ipairs(vim.api.nvim_list_wins()) do
    if vim.fs.normalize(vim.api.nvim_buf_get_name(vim.api.nvim_win_get_buf(w))) == target then
      win = w
      break
    end
  end
  if not win then
    for _, w in ipairs(vim.api.nvim_list_wins()) do
      if M.bufs[vim.api.nvim_win_get_buf(w)] then
        win = w
        break
      end
    end
    win = win or vim.api.nvim_get_current_win()
    vim.api.nvim_set_current_win(win)
    vim.cmd.edit(vim.fn.fnameescape(file))
  end
  vim.api.nvim_set_current_win(win)
  local buf = vim.api.nvim_win_get_buf(win)
  line = math.max(1, math.min(line, vim.api.nvim_buf_line_count(buf)))
  vim.api.nvim_win_set_cursor(win, { line, 0 })
  vim.cmd("normal! zz")
  return "ok"
end

--- Entry point used by `redline open` (nvim -c "lua require('redline').start()").
function M.start(opts)
  if opts then
    M.setup(opts)
  end
  M.attach(vim.api.nvim_get_current_buf())
  vim.api.nvim_create_autocmd("VimLeavePre", { callback = rpc.stop_all })
end

function M.setup(opts)
  M.config = vim.tbl_deep_extend("force", M.config, opts or {})
end

return M
