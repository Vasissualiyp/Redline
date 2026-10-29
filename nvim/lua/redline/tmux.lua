-- Talk to the agent pane (tagged @redline_role=agent by `redline open`).
local M = {}

local function find_pane(role)
  if not vim.env.TMUX then
    return nil
  end
  local r = vim.system({ "tmux", "list-panes", "-s", "-F", "#{pane_id} #{@redline_role}" }, { text = true }):wait()
  for line in (r.stdout or ""):gmatch("[^\n]+") do
    local id, rl = line:match("^(%S+)%s*(%S*)$")
    if rl == role then
      return id
    end
  end
end

function M.send_agent(text)
  local pane = find_pane("agent")
  if not pane then
    vim.fn.setreg("+", text)
    return vim.notify("redline: no agent pane found (start with `redline open`); message copied to clipboard",
      vim.log.levels.WARN)
  end
  -- bracketed paste so multi-line messages are not submitted line by line
  vim.system({ "tmux", "load-buffer", "-b", "redline", "-" }, { stdin = text }):wait()
  vim.system({ "tmux", "paste-buffer", "-p", "-d", "-b", "redline", "-t", pane }):wait()
  vim.defer_fn(function()
    vim.system({ "tmux", "send-keys", "-t", pane, "Enter" })
  end, 150)
  vim.notify("redline: sent to agent")
end

M.find_pane = find_pane
return M
