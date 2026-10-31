-- A "REVIEW" mode in the statusline while a buffer is in review mode.
--
-- lightline.vim (detected): its mode component shows REVIEW, and the mode
-- segment takes M.color, as INSERT or VISUAL take theirs. Visual and insert
-- still show as usual inside review mode.
-- While compiling, the label reads "REVIEW TeX…"; while the paper fails to
-- compile, "REVIEW TeX ✗" (require("redline.autocompile").status() for others).
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
  local tex = M.in_review() and require("redline.autocompile").status() or ""
  return (M.mode() or vim.fn["lightline#mode"]()) .. (tex ~= "" and (" " .. tex) or "")
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
    -- every segment drawn in the mode's colour (the mode name, and in most
    -- themes the line:column block on the right), separators included
    local mode = hl("LightlineLeft_normal_0")
    local mbg, mcbg = mode.bg, mode.ctermbg
    for _, side in ipairs({ "Left", "Right" }) do
      for i = 0, 6 do
        for _, name in ipairs({ ("Lightline%s_normal_%d"):format(side, i),
          ("Lightline%s_normal_%d_%d"):format(side, i, i + 1),
          ("Lightline%s_normal_%d_%d"):format(side, i - 1, i) }) do
          local h = hl(name)
          if next(h) then
            local new = vim.deepcopy(h)
            if mbg and h.bg == mbg then
              new.bg, new.ctermbg = M.color.gui, M.color.cterm
            end
            if mbg and h.fg == mbg then
              new.fg, new.ctermfg = M.color.gui, M.color.cterm
            end
            if mcbg and h.ctermbg == mcbg and not (mbg and h.bg ~= mbg) then
              new.ctermbg = M.color.cterm
            end
            vim.api.nvim_set_hl(0, name, new)
          end
        end
      end
    end
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
