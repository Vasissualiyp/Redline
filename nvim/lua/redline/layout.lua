-- Switch the tmux pane arrangement (presets live in redline/layout.py).
local rpc = require("redline.rpc")

local M = {}

local function run(args)
  vim.system(rpc.shell("layout " .. args), { text = true }, vim.schedule_wrap(function(r)
    local out = ((r.stdout or "") .. (r.stderr or "")):gsub("%s+$", "")
    vim.notify("redline: " .. out, r.code == 0 and vim.log.levels.INFO or vim.log.levels.WARN)
  end))
end

function M.presets()
  local r = vim.system(rpc.shell("layout --list --json"), { text = true }):wait()
  local ok, list = pcall(vim.json.decode, r.stdout or "")
  return ok and list or {}
end

function M.apply(name)
  run(vim.fn.shellescape(name))
end

function M.next(step)
  run(step and step < 0 and "--prev" or "--next")
end

function M.pick()
  vim.ui.select(M.presets(), {
    prompt = "Pane layout",
    format_item = function(p) return ("%-13s %s"):format(p.name, p.desc) end,
  }, function(p)
    if p then
      M.apply(p.name)
    end
  end)
end

return M
