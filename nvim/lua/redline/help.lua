-- The keybind help (`?` in review mode): grouped, coloured by kind, scrollable and searchable.
--
--   j k <C-d> <C-u> <C-e> <C-y> gg G H M L   move (it is an ordinary buffer)
--   /          filter as you type (words are ANDed; matches the key, the text and the group);
--              <CR> keeps the filter and returns to the list, <Esc> clears it
--   <Tab> / <S-Tab>   show one group at a time (cycles through all of them)
--   q  <Esc>  ?       close
local M = {}

local ns = vim.api.nvim_create_namespace("redline_help")

--- Group colours (one-dark flavoured, overridable: `hi RedlineHelpDecide guifg=...`).
local COLORS = {
  Move = "#61afef", Decide = "#98c379", Mark = "#e5c07b", Edit = "#d19a66", Agent = "#c678dd",
  Verify = "#56b6c2", Build = "#e06c75", Sync = "#2bbc8a", Session = "#abb2bf", Note = "#7f848e",
}

local function define_highlights()
  for name, fg in pairs(COLORS) do
    vim.api.nvim_set_hl(0, "RedlineHelp" .. name, { fg = fg, bold = name ~= "Note", default = true })
  end
  vim.api.nvim_set_hl(0, "RedlineHelpDim", { fg = "#7f848e", default = true })
  vim.api.nvim_set_hl(0, "RedlineHelpHead", { fg = "#abb2bf", bold = true, underline = true, default = true })
end

--- spec: { title = "...", groups = { { name = "Decide", hl = "Decide", title = "Accept, reject",
---   entries = { { key = "a", mode = "n", desc = "..." }, ... } }, ... }, notes = { "...", ... } }
function M.open(spec)
  define_highlights()
  local state = { query = "", only = nil }
  local buf = vim.api.nvim_create_buf(false, true)
  vim.bo[buf].bufhidden = "wipe"
  vim.bo[buf].filetype = "redline-help"

  local cols, rows = vim.o.columns, vim.o.lines
  local width = math.min(96, cols - 6)
  local height = math.max(8, math.min(rows - 6, 60))
  local row, col = math.floor((rows - height) / 2) - 1, math.floor((cols - width) / 2)
  local win = vim.api.nvim_open_win(buf, true, {
    relative = "editor", row = math.max(0, row), col = col, width = width, height = height,
    border = "rounded", style = "minimal", title = " Redline keys ", title_pos = "center",
    footer = " / search   <Tab> group   j/k <C-d>/<C-u> scroll   q close ", footer_pos = "center",
  })
  vim.wo[win].cursorline = true
  vim.wo[win].wrap = false

  local function matches(g, e)
    if state.only and g.name ~= state.only then
      return false
    end
    if state.query == "" then
      return true
    end
    local hay = (e.key .. " " .. e.desc .. " " .. g.name .. " " .. (g.title or "")):lower()
    for word in state.query:lower():gmatch("%S+") do
      if not hay:find(word, 1, true) then
        return false
      end
    end
    return true
  end

  local function render()
    local lines, marks, n = {}, {}, 0
    local function add(text, hl, from, to)
      table.insert(lines, text)
      if hl then
        table.insert(marks, { #lines - 1, hl, from or 0, to or -1 })
      end
    end
    for _, g in ipairs(spec.groups) do
      local shown = vim.tbl_filter(function(e) return matches(g, e) end, g.entries)
      if #shown > 0 then
        if #lines > 0 then
          add("")
        end
        add((" %s"):format(g.title or g.name), "RedlineHelp" .. g.hl)
        for _, e in ipairs(shown) do
          n = n + 1
          local mode = e.mode == "x" and "v" or " "
          local keytxt = ("  %s %-7s"):format(mode, e.key)
          local line = keytxt .. " " .. e.desc
          add(line, nil)
          table.insert(marks, { #lines - 1, "RedlineHelp" .. g.hl, 2, #keytxt })
          if e.mode == "x" then
            table.insert(marks, { #lines - 1, "RedlineHelpDim", 2, 3 })
          end
        end
      end
    end
    if state.query == "" and not state.only and spec.notes then
      add("")
      for _, t in ipairs(spec.notes) do
        add("  " .. t, "RedlineHelpNote")
      end
    end
    if n == 0 then
      add("")
      add("  no key matches", "RedlineHelpDim")
    end
    vim.bo[buf].modifiable = true
    vim.api.nvim_buf_set_lines(buf, 0, -1, false, lines)
    vim.bo[buf].modifiable = false
    vim.api.nvim_buf_clear_namespace(buf, ns, 0, -1)
    for _, m in ipairs(marks) do
      vim.api.nvim_buf_add_highlight(buf, ns, m[2], m[1], m[3], m[4])
    end
    local title = " Redline keys"
    if state.only then
      title = title .. "  [" .. state.only .. "]"
    end
    if state.query ~= "" then
      title = title .. "  /" .. state.query .. "  (" .. n .. ")"
    end
    if vim.api.nvim_win_is_valid(win) then
      vim.api.nvim_win_set_config(win, { title = title .. " ", title_pos = "center" })
    end
  end

  local function close()
    if vim.api.nvim_win_is_valid(win) then
      vim.api.nvim_win_close(win, true)
    end
  end

  local function map(lhs, fn)
    vim.keymap.set("n", lhs, fn, { buffer = buf, nowait = true })
  end
  map("q", close)
  map("?", close)
  map("<Esc>", function()
    if state.query ~= "" or state.only then
      state.query, state.only = "", nil
      render()
    else
      close()
    end
  end)

  local function cycle(step)
    local names = {}
    for _, g in ipairs(spec.groups) do
      table.insert(names, g.name)
    end
    local i = 0
    for k, nme in ipairs(names) do
      if nme == state.only then
        i = k
      end
    end
    i = i + step
    if i > #names then
      i = 0
    elseif i < 0 then
      i = #names
    end
    state.only = i > 0 and names[i] or nil
    render()
    vim.api.nvim_win_set_cursor(win, { 1, 0 })
  end
  map("<Tab>", function() cycle(1) end)
  map("<S-Tab>", function() cycle(-1) end)

  -- the search line: a one-line float under the list; the list re-filters as you type
  map("/", function()
    local ibuf = vim.api.nvim_create_buf(false, true)
    vim.bo[ibuf].bufhidden = "wipe"
    local cfg = vim.api.nvim_win_get_config(win)
    local iwin = vim.api.nvim_open_win(ibuf, true, {
      relative = "editor", row = cfg.row + cfg.height + 2, col = cfg.col, width = cfg.width, height = 1,
      border = "rounded", style = "minimal", title = " search ", title_pos = "left",
    })
    vim.api.nvim_buf_set_lines(ibuf, 0, -1, false, { state.query })
    vim.cmd("startinsert!")
    local function back(keep)
      vim.cmd("stopinsert")
      if not keep then
        state.query = ""
        render()
      end
      if vim.api.nvim_win_is_valid(iwin) then
        vim.api.nvim_win_close(iwin, true)
      end
      if vim.api.nvim_win_is_valid(win) then
        vim.api.nvim_set_current_win(win)
        vim.api.nvim_win_set_cursor(win, { 1, 0 })
      end
    end
    vim.api.nvim_create_autocmd({ "TextChangedI", "TextChanged" }, { buffer = ibuf, callback = function()
      state.query = vim.api.nvim_buf_get_lines(ibuf, 0, 1, false)[1] or ""
      render()
    end })
    vim.keymap.set({ "i", "n" }, "<CR>", function() back(true) end, { buffer = ibuf })
    vim.keymap.set({ "i", "n" }, "<Esc>", function() back(false) end, { buffer = ibuf })
    vim.keymap.set("i", "<C-c>", function() back(false) end, { buffer = ibuf })
  end)

  render()
  return win
end

return M
