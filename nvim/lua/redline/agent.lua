-- Switch the agent pane between claude and opencode models (the [models] entries).
local core = require("redline")
local rpc = require("redline.rpc")

local M = {}

local function run(args, root, cb)
  vim.system(rpc.shell(args), { cwd = root, text = true, env = { REDLINE_ROOT = root } }, vim.schedule_wrap(cb))
end

function M.pick()
  local st = core.state()
  if not st then
    return vim.notify("redline: buffer not attached")
  end
  vim.notify("redline: checking the agents…")
  run("agent --list --json", st.root, function(r)
    local ok, items = pcall(vim.json.decode, r.stdout or "")
    if not ok or type(items) ~= "table" then
      return vim.notify("redline agent: " .. vim.trim((r.stderr or "") .. (r.stdout or "")), vim.log.levels.ERROR)
    end
    vim.ui.select(items, {
      prompt = "Agent pane",
      format_item = function(it)
        return (it.current and "● " or "  ") .. it.label .. (it.problem ~= "" and ("  [" .. it.problem .. "]") or "")
      end,
    }, function(it)
      if not it then
        return
      end
      if it.problem ~= "" then
        return vim.notify("redline: " .. it.label .. ": " .. it.problem, vim.log.levels.WARN)
      end
      local q = (it.current and "Restart " or "Switch the agent pane to ") .. it.label
        .. "? The conversation in it ends."
      if vim.fn.confirm(q, "&Yes\n&No", 1) ~= 1 then
        return
      end
      run("agent " .. vim.fn.shellescape(it.name), st.root, function(res)
        local msg = vim.trim((res.stdout or "") .. (res.stderr or ""))
        vim.notify("redline: " .. msg, res.code == 0 and vim.log.levels.INFO or vim.log.levels.WARN)
      end)
    end)
  end)
end

return M
