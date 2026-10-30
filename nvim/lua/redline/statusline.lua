-- A "REVIEW" mode in the statusline while a buffer is in review mode.
--
-- lightline.vim (detected): its mode component shows REVIEW, and the mode
-- segment takes M.color, as INSERT or VISUAL take theirs. Visual and insert
-- still show as usual inside review mode.
-- lualine / other statuslines: use require("redline.statusline").mode(), which is
-- "REVIEW" or nil, e.g. lualine_a = { { "mode", fmt = function(s)
--   return require("redline.statusline").mode() or s end } }
local core = require("redline")

local M = {}

M.color = { gui = "#e5c07b", cterm = 180 } -- the "one" palette's yellow

function M.in_review(buf)
  local st = core.bufs[buf or vim.api.nvim_get_current_buf()]
  return st ~= nil and st.review
end

function M.mode()
  if M.in_review() and vim.fn.mode() == "n" then
    return "REVIEW"
  end
  return nil
end

-- ---- lightline ------------------------------------------------------------------
local hooked = false

local function has_lightline()
  return vim.fn.exists("*lightline#update") == 1 or vim.fn.exists("g:lightline") == 1
end

--- What lightline's mode component shows (called from the statusline).
function M.lightline_label()
  return M.mode() or vim.fn["lightline#mode"]()
end

local function hook_lightline()
  if hooked or not has_lightline() then
    return hooked
  end
  local ok = pcall(vim.fn["lightline#mode"]) -- loads the autoload file
  if not ok then
    return false
  end
  vim.cmd([[
    function! RedlineLightlineMode() abort
      return luaeval('require("redline.statusline").lightline_label()')
    endfunction
  ]])
  local g = vim.g.lightline or {}
  g.component_function = g.component_function or {}
  if g.component_function.mode == nil then
    g.component_function.mode = "RedlineLightlineMode"
  end
  vim.g.lightline = g
  vim.fn["lightline#init"]()
  vim.fn["lightline#colorscheme"]()
  vim.fn["lightline#update"]()
  hooked = true
  return true
end

local function hl(name)
  local ok, h = pcall(vim.api.nvim_get_hl, 0, { name = name, link = false })
  return ok and h or {}
end

--- Colour the mode segment for the current buffer: REVIEW's colour, or normal's again.
function M.paint()
  if not hooked then
    return
  end
  if M.in_review() then
    local seg, sep = hl("LightlineLeft_normal_0"), hl("LightlineLeft_normal_0_1")
    vim.api.nvim_set_hl(0, "LightlineLeft_normal_0", vim.tbl_extend("force", seg,
      { bg = M.color.gui, ctermbg = M.color.cterm }))
    vim.api.nvim_set_hl(0, "LightlineLeft_normal_0_1", vim.tbl_extend("force", sep,
      { fg = M.color.gui, ctermfg = M.color.cterm }))
  else
    pcall(vim.fn["lightline#highlight"], "normal")
  end
  vim.cmd("redrawstatus")
end

--- Called when a buffer enters or leaves review mode.
function M.update()
  if hook_lightline() then
    M.paint()
  else
    vim.cmd("redrawstatus")
  end
  vim.api.nvim_exec_autocmds("User", { pattern = "RedlineReview", modeline = false })
end

local group = vim.api.nvim_create_augroup("RedlineStatusline", { clear = true })
vim.api.nvim_create_autocmd({ "BufEnter", "WinEnter" }, {
  group = group,
  callback = function()
    if hooked then
      M.paint()
    end
  end,
})
vim.api.nvim_create_autocmd("ColorScheme", {
  group = group,
  callback = function()
    vim.schedule(function()
      if hooked then
        M.paint()
      end
    end)
  end,
})

return M
