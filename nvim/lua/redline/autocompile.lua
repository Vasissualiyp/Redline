-- Compile by itself once the paper has been quiet for `auto_compile` seconds.
--
-- Every edit (yours, or an agent's arriving from disk) restarts the countdown;
-- typing in insert mode does not start it until you leave insert mode. Only one
-- compile runs at a time: edits made during one queue exactly one more. Results
-- are quiet: errors become diagnostics, and a message appears only when the
-- paper stops or starts compiling. The PDF pane keeps the last clean PDF.
local core = require("redline")

local M = {}

local state = { enabled = nil, timer = nil, running = false, pending = false, buf = nil, ok = nil }

function M.enabled()
  if state.enabled == nil then
    state.enabled = (core.config.auto_compile or 0) > 0
  end
  return state.enabled
end

local function fire()
  local buf = state.buf
  if not (buf and vim.api.nvim_buf_is_valid(buf) and core.bufs[buf]) then
    return
  end
  if vim.fn.mode():match("^[iR]") then
    return -- InsertLeave starts the countdown again
  end
  if state.running then
    state.pending = true
    return
  end
  state.running = true
  vim.cmd("redrawstatus")
  require("redline.review").compile(function(res)
    state.running = false
    if res then
      local nerr = #(res.errors or {})
      if not res.ok and state.ok ~= false then
        local first = res.errors and res.errors[1]
        vim.notify(("redline: auto-compile failed (%d error%s%s); diagnostics show where, %s sends them to the agent")
          :format(nerr, nerr == 1 and "" or "s", first and (": " .. first.message:sub(1, 80)) or "",
            core.config.prefix .. "F"), vim.log.levels.WARN)
      elseif res.ok and state.ok == false then
        vim.notify("redline: compiles again")
      end
      state.ok = res.ok
    end
    vim.cmd("redrawstatus")
    if state.pending then
      state.pending = false
      M.poke(buf)
    end
  end, { quiet = true, buf = buf })
end

--- An edit happened: (re)start the countdown.
function M.poke(buf)
  if not M.enabled() then
    return
  end
  state.buf = buf or vim.api.nvim_get_current_buf()
  state.timer = state.timer or vim.uv.new_timer()
  state.timer:stop()
  state.timer:start(math.floor(core.config.auto_compile * 1000), 0, vim.schedule_wrap(fire))
end

function M.toggle()
  state.enabled = not M.enabled()
  if not state.enabled and state.timer then
    state.timer:stop()
  end
  if state.enabled and (core.config.auto_compile or 0) <= 0 then
    core.config.auto_compile = 10
  end
  vim.notify(("redline: auto-compile %s"):format(state.enabled
    and ("on (" .. core.config.auto_compile .. " s after the last edit)") or "off"))
end

--- For statuslines: "" when off/idle and fine, else "TeX…" while compiling or "TeX ✗".
function M.status()
  if state.running then
    return "TeX…"
  end
  if state.ok == false then
    return "TeX ✗"
  end
  return ""
end

return M
