-- An animated spinner for things that take a while (compile, Overleaf sync).
--   local done = require("redline.spinner").start("compiling")   -- ... done() when finished
-- The statusline's mode label shows the frame and the job (see statusline.lua); the
-- statusline is redrawn ~8 times a second while a job runs and not at all otherwise.
local M = {}

local FRAMES = { "⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏" }
local jobs, timer, n = {}, nil, 0

local function tick()
  if next(jobs) == nil then
    if timer then
      timer:stop()
    end
    return
  end
  vim.cmd("redrawstatus")
end

--- Start a named job; returns the function that ends it.
function M.start(label)
  n = n + 1
  local id = n
  jobs[id] = label
  timer = timer or vim.uv.new_timer()
  timer:start(0, 120, vim.schedule_wrap(tick))
  return function()
    jobs[id] = nil
    vim.schedule(function()
      vim.cmd("redrawstatus")
    end)
  end
end

--- "⠹ compiling" for the statusline, or "" when nothing runs.
function M.label()
  local label = select(2, next(jobs))
  if not label then
    return ""
  end
  return FRAMES[(math.floor(vim.uv.hrtime() / 1e8) % #FRAMES) + 1] .. " " .. label
end

function M.active()
  return next(jobs) ~= nil
end

return M
