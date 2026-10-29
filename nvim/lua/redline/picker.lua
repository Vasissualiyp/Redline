-- Change list: Telescope when available, vim.ui.select otherwise.
local core = require("redline")

local M = {}

local function line_of(it)
  local m = it.marker and ("%" .. it.marker.kind .. " ") or ""
  local text = it.new or it.old or it.comment or ""
  text = text:gsub("%s+", " ")
  return ("%-4s %-8s %s§%s  %s"):format(it.label, it.kind, m, it.section, text:sub(1, 90))
end

local function details_lines(it)
  local out = { ("%s  %s  (%s)  §%s  lines %d-%d  origin: %s"):format(it.label, it.kind, it.id,
    it.section, it.lines[1], it.lines[2], it.origin) }
  if it.marker then
    table.insert(out, ("marker: %%%s %s"):format(it.marker.kind, it.marker.note or ""))
  end
  for _, k in ipairs({ "new", "old", "comment" }) do
    if it[k] then
      table.insert(out, "")
      table.insert(out, k .. ":")
      for _, l in ipairs(vim.split(it[k], "\n", { plain = true })) do
        table.insert(out, "  " .. l)
      end
    end
  end
  return out
end

function M.details(it)
  if not it then
    return vim.notify("redline: no change under the cursor")
  end
  local lines = details_lines(it)
  local b = vim.api.nvim_create_buf(false, true)
  vim.api.nvim_buf_set_lines(b, 0, -1, false, lines)
  vim.bo[b].filetype = "tex"
  local width = math.min(110, vim.o.columns - 4)
  local w = vim.api.nvim_open_win(b, false, { relative = "cursor", row = 1, col = 0, width = width,
    height = math.min(#lines + 4, 25), border = "rounded", style = "minimal" })
  vim.wo[w].wrap = true
  vim.api.nvim_create_autocmd({ "CursorMoved", "BufLeave" }, { once = true, callback = function()
    pcall(vim.api.nvim_win_close, w, true)
  end })
end

local function jump(it)
  vim.api.nvim_win_set_cursor(0, { it.range[1] + 1, it.range[2] })
  vim.cmd("normal! zz")
end

function M.pick(filter)
  local st = core.state()
  if not (st and st.data) then
    return vim.notify("redline: buffer not attached")
  end
  local list = {}
  for _, it in ipairs(st.data.items) do
    if not filter or filter(it) then
      table.insert(list, it)
    end
  end
  local ok, pickers = pcall(require, "telescope.pickers")
  if not ok then
    return vim.ui.select(list, { prompt = "changes", format_item = line_of }, function(it)
      if it then jump(it) end
    end)
  end
  local finders = require("telescope.finders")
  local conf = require("telescope.config").values
  local actions = require("telescope.actions")
  local state = require("telescope.actions.state")
  local previewers = require("telescope.previewers")
  pickers.new({}, {
    prompt_title = "redline changes",
    finder = finders.new_table({
      results = list,
      entry_maker = function(it)
        return { value = it, display = line_of(it), ordinal = line_of(it) .. " " .. (it.origin or "") }
      end,
    }),
    sorter = conf.generic_sorter({}),
    previewer = previewers.new_buffer_previewer({
      define_preview = function(self, entry)
        vim.api.nvim_buf_set_lines(self.state.bufnr, 0, -1, false, details_lines(entry.value))
        vim.bo[self.state.bufnr].filetype = "tex"
        vim.wo[self.state.winid].wrap = true
      end,
    }),
    attach_mappings = function(bufnr)
      actions.select_default:replace(function()
        local e = state.get_selected_entry()
        actions.close(bufnr)
        if e then jump(e.value) end
      end)
      return true
    end,
  }):find()
end

return M
