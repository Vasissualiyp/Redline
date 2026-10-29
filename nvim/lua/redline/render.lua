-- Draws parsed changes as extmarks: colours, conceal, signs, diagnostics.
local M = {}

M.ns = vim.api.nvim_create_namespace("redline")
M.diag_ns = vim.api.nvim_create_namespace("redline_lint")

local function hl_fg(name, fallback)
  local ok, h = pcall(vim.api.nvim_get_hl, 0, { name = name, link = false })
  return (ok and h and h.fg) or fallback
end

function M.define_highlights()
  local green = hl_fg("DiagnosticOk", hl_fg("String", 0x98c379))
  local red = hl_fg("DiagnosticError", 0xe06c75)
  local yellow = hl_fg("DiagnosticWarn", 0xe5c07b)
  local blue = hl_fg("DiagnosticInfo", 0x61afef)
  local grey = hl_fg("Comment", 0x7f848e)
  local set = function(name, spec)
    spec.default = true
    vim.api.nvim_set_hl(0, name, spec)
  end
  set("RedlineAdded", { fg = green, bold = true })
  set("RedlineDeleted", { fg = red, strikethrough = true })
  set("RedlineHighlight", { fg = yellow, underline = true })
  set("RedlineComment", { fg = blue, italic = true })
  set("RedlineMacro", { fg = grey })
  set("RedlineMarkerN", { fg = yellow, bold = true, reverse = true })
  set("RedlineMarkerX", { fg = red, bold = true, reverse = true })
  set("RedlineMarkerY", { fg = green, bold = true, reverse = true })
  set("RedlineLabel", { fg = grey, italic = true })
  set("RedlineSignAdded", { fg = green })
  set("RedlineSignDeleted", { fg = red })
  set("RedlineSignReplaced", { fg = yellow })
  set("RedlineSignComment", { fg = blue })
  set("RedlineVerified", { fg = green })
end

local SIGN = {
  added = { "+", "RedlineSignAdded" },
  deleted = { "-", "RedlineSignDeleted" },
  replaced = { "~", "RedlineSignReplaced" },
  comment = { "‡", "RedlineSignComment" },
  highlight = { "!", "RedlineSignReplaced" },
}
local ARG_HL = {
  added = { "RedlineAdded" },
  deleted = { "RedlineDeleted" },
  replaced = { "RedlineAdded", "RedlineDeleted" },
  comment = { "RedlineComment" },
  highlight = { "RedlineHighlight" },
}

local function mark(buf, r, opts)
  opts.end_row = r[3]
  opts.end_col = r[4]
  opts.strict = false
  pcall(vim.api.nvim_buf_set_extmark, buf, M.ns, r[1], r[2], opts)
end

local function conceal(buf, r, char, enabled)
  if r[1] == r[3] and r[2] == r[4] then
    return
  end
  if enabled then
    mark(buf, r, { conceal = char, priority = 200 })
  else
    mark(buf, r, { hl_group = "RedlineMacro", priority = 150 })
  end
end

--- Draw `data` (the result of the rpc `parse` method) into buf.
function M.draw(buf, data, opts)
  vim.api.nvim_buf_clear_namespace(buf, M.ns, 0, -1)
  local concealing = opts.conceal
  local sign_lines = {}
  for _, it in ipairs(data.items or {}) do
    local hls = ARG_HL[it.kind] or { "RedlineAdded" }
    for i, a in ipairs(it.args or {}) do
      mark(buf, a, { hl_group = hls[i] or hls[1], priority = 150 })
    end
    local c = it.conceal or {}
    for i, r in ipairs(c) do
      local char = ""
      if i == 1 and it.kind == "comment" then
        char = "‡"
      elseif i > 1 and i < #c then
        char = "←" -- between new and old in \replaced
      end
      conceal(buf, r, char, concealing)
    end
    if it.comment_range then
      mark(buf, it.comment_arg, { hl_group = "RedlineComment", priority = 150 })
      conceal(buf, it.comment_conceal[1], "‡", concealing)
      conceal(buf, it.comment_conceal[2], "", concealing)
    end
    local s = SIGN[it.kind] or SIGN.added
    local sign, sign_hl = s[1], s[2]
    if it.marker then
      local mhl = "RedlineMarker" .. it.marker.kind
      sign, sign_hl = it.marker.kind, mhl
      if it.marker_range then
        mark(buf, it.marker_range, { hl_group = mhl, priority = 160 })
      end
    end
    for row = it.full_range[1], it.full_range[3] do
      if not sign_lines[row] then
        sign_lines[row] = true
        pcall(vim.api.nvim_buf_set_extmark, buf, M.ns, row, 0, {
          sign_text = row == it.full_range[1] and sign or "│",
          sign_hl_group = sign_hl,
          priority = 20,
        })
      end
    end
    if opts.labels then
      local label = it.label .. (it.origin ~= "manual" and (" " .. it.origin) or "")
      if it.marker and it.marker.note and it.marker.note ~= "" then
        label = label .. "  %" .. it.marker.kind .. " " .. it.marker.note
      end
      pcall(vim.api.nvim_buf_set_extmark, buf, M.ns, it.full_range[3], 0, {
        virt_text = { { "  " .. label, "RedlineLabel" } },
        virt_text_pos = "eol",
        priority = 10,
      })
    end
  end
  for _, row in ipairs(data.verified_lines or {}) do
    if not sign_lines[row] then
      pcall(vim.api.nvim_buf_set_extmark, buf, M.ns, row, 0,
        { sign_text = "✓", sign_hl_group = "RedlineVerified", priority = 5 })
    end
  end
  -- lint issues as diagnostics
  local diags = {}
  for _, iss in ipairs(data.lint or {}) do
    if iss.line and iss.line > 0 then
      table.insert(diags, {
        lnum = iss.line - 1,
        col = 0,
        severity = iss.severity == "error" and vim.diagnostic.severity.ERROR or vim.diagnostic.severity.WARN,
        source = "redline",
        code = iss.code,
        message = iss.message .. (iss.fixable and " (fixable: :Redline lint fix)" or ""),
      })
    end
  end
  vim.diagnostic.set(M.diag_ns, buf, diags)
end

return M
